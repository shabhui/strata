"""验证打包产物:跑一遍冻结后的代码路径。

为什么要单独一个脚本:发布用的 exe 里写了 requireAdministrator,从普通
命令行根本起不来(Permission denied),所以没法直接自动化验它。
manifest 和「冻结后的 Python 代码能不能跑」是两件独立的事 —— 这里用同一份
配置、只去掉 UAC 要求,打一个临时 exe 出来验后者。

manifest 本身另外验(读发布 exe 里嵌的资源,见下面 check_manifest)。

验的是那些只有冻结之后才会暴露的问题:
  - web/ 有没有被打进去,config.web_dir() 在 _MEIPASS 里能不能找到
  - 那些运行时才 import 的模块有没有漏
  - 数据库路径有没有跟着跑到临时目录里去(必须还在 %LOCALAPPDATA%)
  - 服务能不能起来、能不能把界面发出去
"""

from __future__ import annotations

import json
import locale
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RELEASE_EXE = ROOT / "dist" / "Strata.exe"
PORT = 8765                     # 避开默认 8731 和预览用的 8732


def log(msg: str) -> None:
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()


# 「没验成」和「验过了没问题」是两件事。攒在这儿,最后一行别说满话 ——
# 警告埋在几十行输出中间等于没有,末尾那句「可以发出去了」才是人看的。
caveats: list[str] = []


def check_fresh() -> bool:
    """dist/Strata.exe 是不是当前代码打出来的。

    下面几组验的都是「这个 exe 能跑」,没有一组能回答「这个 exe 是哪版代码」。
    改了 src/ 忘了重新打包,整套照样通过,末尾照样打「可以发出去了」—— 报告
    是真的,只是说的是一个旧二进制。和刚修掉的那条界面目录检查同一类毛病:
    检查过了,但过的不是你以为的那件事。
    """
    log("\n[1/6] 发布 exe 和当前代码对不对得上")
    if not RELEASE_EXE.exists():
        log(f"  跳过:{RELEASE_EXE.name} 不在")
        return False

    sys.path.insert(0, str(ROOT / "tools"))
    import sources

    verdict, diff = sources.compare()
    if verdict == "missing":
        # 上一次打包的时候还没有这套机制。不算失败 —— 但也不能装作验过了,
        # 所以记一条 caveat,末尾那句话会跟着改。
        log("  ?? 没有源码指纹(dist/Strata.sources.json 不在)")
        log("     这个 exe 打包时还没有这套记录,验不了它对应哪版代码。")
        log("     重新打一次包就有了:python tools/build_exe.py")
        for line in diff:
            log(f"     {line}")
        caveats.append("没验出 exe 对应哪版代码(缺源码指纹)")
        return True
    if verdict == "stale":
        log(f"  BAD exe 比代码旧,有 {len(diff)} 处不一致 —— 得重新打包")
        for line in diff[:12]:
            log(f"       {line}")
        if len(diff) > 12:
            log(f"       …… 还有 {len(diff) - 12} 处")
        log("     下面几组验的会是这个旧 exe,通过了也不代表当前代码没问题。")
        return False
    log(f"  OK  一致({len(sources.fingerprint())} 个文件,"
        f"exe {RELEASE_EXE.stat().st_size / 1048576:.1f} MB)")
    return True


def check_manifest() -> bool:
    """发布 exe 里该有的两样东西:强制提权、长路径。"""
    log("\n[2/6] 发布 exe 的 manifest")
    if not RELEASE_EXE.exists():
        log(f"  跳过:{RELEASE_EXE} 不在,先跑 tools/build_exe.py")
        return False
    try:
        from PyInstaller.utils.win32 import winmanifest
        raw = winmanifest.read_manifest_from_executable(str(RELEASE_EXE))
    except Exception as exc:                          # noqa: BLE001
        log(f"  读不出来:{type(exc).__name__}: {exc}")
        return False

    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    ok = True
    for probe, why in (("requireAdministrator", "双击就提权"),
                       ("longPathAware", "超过 260 字符的路径")):
        good = probe in text
        ok &= good
        log(f"  {'OK ' if good else 'BAD'} {probe}({why})")
    return ok


def build_test_exe(workdir: Path) -> Path | None:
    """用发布配置打一个不要求提权的副本,只为了能在普通权限下跑。"""
    log("\n[3/6] 打一个不提权的副本用来验代码路径")
    spec_src = (ROOT / "tools" / "strata.spec").read_text(encoding="utf-8")
    # 只动这两处:去掉 UAC 要求和自定义 manifest,其余(datas、hiddenimports、
    # excludes)和发布配置完全一致,不然验的就不是同一个东西了。
    spec = (spec_src
            .replace("uac_admin=True", "uac_admin=False")
            .replace('manifest=str(ROOT / "tools" / "strata.manifest"),', "")
            .replace('name="Strata"', 'name="StrataNoUAC"'))
    spec_path = workdir / "strata_nouac.spec"
    # spec 里用 SPECPATH 推 ROOT,所以它必须待在 tools/ 的位置上
    spec_path = ROOT / "tools" / "_verify_nouac.spec"
    spec_path.write_text(spec, encoding="utf-8")

    try:
        proc = subprocess.run(
            [sys.executable, "-m", "PyInstaller", str(spec_path), "--noconfirm",
             "--distpath", str(workdir / "dist"), "--workpath", str(workdir / "build"),
             "--log-level", "WARN"],
            cwd=str(ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
    finally:
        spec_path.unlink(missing_ok=True)

    exe = workdir / "dist" / "StrataNoUAC.exe"
    if proc.returncode != 0 or not exe.exists():
        log(f"  打包失败(返回码 {proc.returncode})")
        log((proc.stderr or proc.stdout)[-1500:])
        return None
    log(f"  好了:{exe.name},{exe.stat().st_size / 1048576:.1f} MB")
    return exe


def decode_output(raw: bytes) -> str:
    """按子进程真正用的编码解,而不是按我们希望的那个。

    冻结后的 exe 不认 PYTHONIOENCODING —— 实测在中文 Windows 上它往管道写的是
    GBK(本地代码页)。按 UTF-8 硬解,整段中文全变成问号,于是下面每一条中文
    判据都永远不成立。这不只是日志难看:

      if "警告:界面目录不存在" in out:  ← 永远不成立
          BAD
      else:
          OK "界面目录找得到(web/ 打进去了)"  ← 于是永远走这里

    web/ 真没打进去也照样报通过。检查哑掉的方式是"通过",比没有检查更糟。
    数据库那条当初写成 endswith("strata.db") 才躲过一劫 —— 判据是 ASCII 的。

    先试 UTF-8:哪天 PyInstaller 认了这个变量,或者换到英文机器上,拿到的就是
    UTF-8,该按 UTF-8 解。GBK 编的中文散文几乎不可能同时是合法 UTF-8,所以
    这个顺序不会误判。
    """
    for codec in ("utf-8", locale.getpreferredencoding(False), "cp936"):
        try:
            return raw.decode(codec)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def run(exe: Path, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    """跑子命令,拿字节回来自己解。

    还是照旧设 PYTHONIOENCODING:它对源码模式有效,冻结后无效(见
    decode_output),两种情况下 decode_output 都能解对。
    """
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    done = subprocess.run(
        [str(exe), *args], capture_output=True, timeout=timeout, env=env,
    )
    return subprocess.CompletedProcess(
        done.args, done.returncode,
        decode_output(done.stdout or b""), decode_output(done.stderr or b""),
    )


def check_doctor(exe: Path) -> bool:
    """doctor 会把界面目录、数据库路径都打出来,一条命令能验好几件事。"""
    log("\n[4/6] 冻结后的 doctor")
    try:
        proc = run(exe, "doctor")
    except subprocess.TimeoutExpired:
        log("  超时")
        return False
    if proc.returncode != 0:
        log(f"  返回码 {proc.returncode}")
        log((proc.stderr or proc.stdout)[-1200:])
        return False

    out = proc.stdout
    ok = True

    # 界面目录:两头都要查。
    # 只查"有没有喊警告"不够 —— 那是个反向判据,措辞一改它就哑,而哑掉的方式
    # 是通过。正向再要一条:doctor 打出来的那个路径必须落在 _MEIPASS 里。
    # 落在源码树上说明 config.bundle_dir() 在冻结环境里没认出自己被打包了,
    # 那么发到别人机器上就是 404 —— 在我们这台开发机上却看不出任何问题。
    web_lines = [ln.strip() for ln in out.splitlines()
                 if ln.strip().lower().endswith("web")]
    if "警告:界面目录不存在" in out:
        log("  BAD web/ 没打进去 —— serve 会全 404")
        ok = False
    elif not web_lines:
        log("  BAD doctor 输出里找不到界面目录那一行(格式变了?)")
        ok = False
    elif "_MEI" not in web_lines[0]:
        log(f"  BAD 界面目录没落在解包目录里:{web_lines[0]}")
        ok = False
    else:
        log(f"  OK  界面目录在解包目录里({web_lines[0].split(':', 1)[-1]})")

    # 数据库不能跟着跑到 _MEIPASS —— 那是临时目录,进程一退就没了,
    # 用户的历史快照会每次都丢。
    # 认路径本身而不认中间的中文标签:标签一改这里就瞎了,而 .db 结尾的
    # 那行路径是我们真正要检查的东西。
    db_lines = [ln.strip() for ln in out.splitlines()
                if ln.strip().lower().endswith("strata.db")]
    if db_lines:
        db = db_lines[0].split(":", 1)[-1].strip()
        good = "AppData" in db and "_MEI" not in db
        log(f"  {'OK ' if good else 'BAD'} 数据库落在 {db}")
        ok &= good
    else:
        log("  BAD doctor 输出里找不到 .db 路径。实际输出:")
        for ln in out.splitlines()[:12]:
            log(f"       | {ln.rstrip()}")
        ok = False

    # 盘和快照都读到了,说明 store/ntfs 那几个模块在冻结环境里 import 成功
    if "读不到" in out:
        log("  注意:有盘读不到(可能是没提权,普通权限下也正常)")
    if "快照" in out:
        log("  OK  数据库读得动(快照信息出来了)")
    return ok


def check_subcommands(exe: Path) -> bool:
    """每个子命令都会 import 一批模块,漏打包的话这里就崩。"""
    log("\n[5/6] 各子命令的 import(冻结后最容易漏这个)")
    ok = True
    for args, expect in (
        # 探测串一律用 ASCII:中文会被 argparse 按宽度折行,用它当判据容易误报。
        (["--help"], "{serve,scan,schedule,doctor}"),
        (["scan", "--help"], "--drives"),
        (["serve", "--help"], "--no-browser"),
        (["schedule", "--help"], "--at"),
        (["schedule", "status"], None),          # 真跑,会 import schedule 模块
    ):
        label = " ".join(args)
        try:
            proc = run(exe, *args, timeout=60)
        except subprocess.TimeoutExpired:
            log(f"  BAD {label}:超时")
            ok = False
            continue
        blob = proc.stdout + proc.stderr
        bad = ("Traceback" in blob or "ModuleNotFoundError" in blob
               or "ImportError" in blob)
        if bad:
            log(f"  BAD {label}:{blob.strip().splitlines()[-1][:120]}")
            ok = False
            continue
        if expect and expect not in blob:
            log(f"  BAD {label}:输出里没有预期内容 {expect!r}")
            ok = False
            continue
        log(f"  OK  {label}")
    return ok


def kill_tree(proc: subprocess.Popen) -> None:
    """把整棵进程树杀干净。

    单文件 exe 的 bootloader 会先把自己解包,再起一个子进程跑真正的程序。
    只 terminate() 父进程的话,那个子进程还活着,继续占着端口 ——
    下次验就起不来了,而且它接着写我们的管道,可能把调用方也卡住。
    所以走 taskkill /T,连子进程一起收掉。
    """
    if proc.poll() is None:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    # 关掉管道,免得读端还挂在上面
    if proc.stdout:
        proc.stdout.close()


def check_serve(exe: Path) -> bool:
    """真起一次服务,把界面和各个接口拉下来看。"""
    log("\n[6/6] 冻结后的 serve(真起服务)")
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    # 二进制管道 + 自己解:进程提前退的时候要读它的输出,那正是最需要看清
    # 中文的时刻(端口占用、库锁住之类都是中文提示)。
    proc = subprocess.Popen(
        [str(exe), "serve", "--port", str(PORT), "--no-browser"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
    )
    base = f"http://127.0.0.1:{PORT}"
    try:
        # 单文件 exe 冷启动要先解包,给它几秒
        ready = False
        for _ in range(60):
            if proc.poll() is not None:
                log(f"  进程提前退了(返回码 {proc.returncode})")
                log(decode_output(proc.stdout.read() if proc.stdout else b"")[-1200:])
                return False
            try:
                with urllib.request.urlopen(base + "/", timeout=2) as r:
                    if r.status == 200:
                        ready = True
                        break
            except (urllib.error.URLError, OSError):
                time.sleep(0.5)
        if not ready:
            log("  起不来(30 秒内没响应)")
            return False
        log("  OK  服务起来了")

        ok = True
        # 界面四件套:HTML/JS/CSS/文案 都得从 _MEIPASS 里发得出来。
        # i18n.js 单独列一条:它 404 的话 app.js 里的 t() 全炸,页面一片空白,
        # 而上面三个照样 200 —— 只验三个的话这种全白能过关。
        # 判据用 'STRINGS' 而不是 'function':i18n.js 就是一张表,
        # 换成箭头函数写法就没有 function 这个词了。
        for path, probe in (("/", "<html"), ("/app.js", "function"),
                            ("/app.css", "{"), ("/i18n.js", "strings")):
            try:
                with urllib.request.urlopen(base + path, timeout=5) as r:
                    body = r.read().decode("utf-8", "replace")
                good = r.status == 200 and probe in body.lower()
                log(f"  {'OK ' if good else 'BAD'} {path}({len(body):,} 字节)")
                ok &= good
            except Exception as exc:                  # noqa: BLE001
                log(f"  BAD {path}:{type(exc).__name__}: {exc}")
                ok = False

        # 每个接口都打一遍。它们分别走 analysis/、store/、ntfs/ 里不同的模块,
        # 漏打包的模块只有在真正调到它的那个接口上才会现形。
        for path in ("/api/status?drive=C:",
                     "/api/timeline?drive=C:&days=30",
                     "/api/tree?drive=C:",
                     "/api/hotspots?drive=C:",
                     "/api/diff?drive=C:",
                     "/api/snapshots?drive=C:",
                     "/api/changes?drive=C:",
                     "/api/scan/state",
                     "/api/schedule"):
            try:
                with urllib.request.urlopen(base + path, timeout=60) as r:
                    data = json.loads(r.read().decode("utf-8"))
                keys = ", ".join(list(data)[:4]) if isinstance(data, dict) else f"{len(data)} 项"
                log(f"  OK  {path} → {keys}")
            except Exception as exc:                  # noqa: BLE001
                log(f"  BAD {path}:{type(exc).__name__}: {exc}")
                ok = False

        # reveal 只验它的拒绝路径 —— 成功路径会真弹资源管理器窗口出来
        try:
            req = urllib.request.Request(
                base + "/api/reveal", method="POST",
                data=json.dumps({"drive": "C:", "path": "Strata-verify-no-such"}).encode(),
                headers={"Content-Type": "application/json", "Origin": base},
            )
            urllib.request.urlopen(req, timeout=10)
            log("  BAD /api/reveal 对不存在的路径居然成功了")
            ok = False
        except urllib.error.HTTPError as err:
            good = err.code == 404
            log(f"  {'OK ' if good else 'BAD'} /api/reveal 不存在的路径 → {err.code}")
            ok &= good
        except Exception as exc:                      # noqa: BLE001
            log(f"  BAD /api/reveal:{type(exc).__name__}: {exc}")
            ok = False
        return ok
    finally:
        kill_tree(proc)


def main() -> int:
    if sys.platform != "win32":
        log("只在 Windows 上有意义。")
        return 1

    log("验证 Strata 打包产物")
    log("=" * 52)

    results = {"fresh": check_fresh(), "manifest": check_manifest()}
    workdir = Path(tempfile.mkdtemp(prefix="tc-verify-"))
    try:
        exe = build_test_exe(workdir)
        if exe is None:
            results["build"] = False
        else:
            results["doctor"] = check_doctor(exe)
            results["subcommands"] = check_subcommands(exe)
            results["serve"] = check_serve(exe)
    finally:
        # ignore_errors 会在文件被占用时默默放弃,留下 9 MB 在临时目录里。
        # 说一声,不然攒起来没人知道。
        shutil.rmtree(workdir, ignore_errors=True)
        if workdir.exists():
            log(f"\n注意:临时目录没删掉(可能还有进程占着):{workdir}")

    log("\n" + "=" * 52)
    bad = [k for k, v in results.items() if not v]
    for name, good in results.items():
        log(f"  {'通过' if good else '失败'}  {name}")
    if bad:
        log(f"\n有问题:{', '.join(bad)}")
        return 1
    if caveats:
        # 每一条都通过了,但有东西根本没验上。说「全部通过」不算撒谎,
        # 说「可以发出去了」就过头了。
        log("\n每条检查都过了,但有没验上的:")
        for line in caveats:
            log(f"  ?? {line}")
        log("\n先把上面这条解决掉,再决定要不要发出去。")
        return 0
    log("\n全部通过。dist/Strata.exe 可以发出去了。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
