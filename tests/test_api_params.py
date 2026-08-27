"""API 入参解析:盘符规整化、整数夹取、非法值报错。

这些函数直接吃 URL 里的东西,是外面能碰到的第一层,坏值不能穿进去。
"""

from __future__ import annotations

import unittest

from strata import config
from strata.server import api
from strata.store import db


def q(**kw: str) -> dict[str, list[str]]:
    """把关键字参数拼成 parse_qs 的形状。"""
    return {k: [v] for k, v in kw.items()}


class DriveParamTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)

    def test_normalizes_to_letter_colon(self) -> None:
        for raw in ("c", "C", "c:", "C:", "c:\\", "C:\\", " c: ", "c:\\\\"):
            with self.subTest(raw=raw):
                self.assertEqual(api._drive_param(q(drive=raw), self.conn), "C:")

    def test_missing_falls_back_to_known_drive(self) -> None:
        db.insert_snapshot(
            self.conn,
            db.Snapshot(
                drive="D:", taken_at=1.0, method="mft", total_bytes=1,
                free_bytes=1, used_bytes=1, scanned_bytes=1, complete=True,
            ),
        )
        self.conn.commit()
        self.assertEqual(api._drive_param({}, self.conn), "D:")

    def test_missing_with_empty_db_uses_default(self) -> None:
        self.assertEqual(
            api._drive_param({}, self.conn), config.DEFAULT_DRIVES[0]
        )

    def test_blank_string_treated_as_missing(self) -> None:
        self.assertEqual(
            api._drive_param(q(drive="   "), self.conn), config.DEFAULT_DRIVES[0]
        )

    def test_rejects_junk(self) -> None:
        for raw in ("1", "..", "CD", "c d", "\\\\server\\share", "%", "-"):
            with self.subTest(raw=raw):
                with self.assertRaises(api.ApiError) as caught:
                    api._drive_param(q(drive=raw), self.conn)
                self.assertEqual(caught.exception.status, 400)

    def test_never_returns_a_path(self) -> None:
        """规整化之后只能是两个字符,不能夹带路径。"""
        for raw in ("c:\\Windows", "c:/Windows/System32"):
            with self.subTest(raw=raw):
                with self.assertRaises(api.ApiError):
                    api._drive_param(q(drive=raw), self.conn)


class IntParamTest(unittest.TestCase):
    def test_default_when_absent_or_blank(self) -> None:
        self.assertEqual(api._int_param({}, "days", 90), 90)
        self.assertEqual(api._int_param(q(days=""), "days", 90), 90)
        self.assertEqual(api._int_param(q(days="  "), "days", 90), 90)

    def test_parses_and_trims(self) -> None:
        self.assertEqual(api._int_param(q(days=" 30 "), "days", 90), 30)

    def test_clamps_to_range(self) -> None:
        self.assertEqual(api._int_param(q(n="0"), "n", 5, lo=1, hi=50), 1)
        self.assertEqual(api._int_param(q(n="-999"), "n", 5, lo=1, hi=50), 1)
        self.assertEqual(api._int_param(q(n="9999"), "n", 5, lo=1, hi=50), 50)
        self.assertEqual(api._int_param(q(n="25"), "n", 5, lo=1, hi=50), 25)

    def test_unicode_digits_parse_and_clamp(self) -> None:
        """int() 认非 ASCII 的十进制数字,这没关系 —— 结果照样被夹到范围里。"""
        self.assertEqual(api._int_param(q(n="٣"), "n", 5, lo=1, hi=50), 3)

    def test_rejects_non_integers(self) -> None:
        for raw in ("abc", "3.5", "1e3", "0x10", "9,9", "inf", "nan", "٣.٥"):
            with self.subTest(raw=raw):
                with self.assertRaises(api.ApiError) as caught:
                    api._int_param(q(n=raw), "n", 5)
                self.assertEqual(caught.exception.status, 400)
                self.assertIn("n", caught.exception.message)

    def test_huge_value_clamped_not_crashed(self) -> None:
        """别让一个大数字变成 90 天 × 巨大循环。"""
        self.assertEqual(
            api._int_param(q(days="9" * 400), "days", 90, lo=1, hi=3650), 3650
        )


if __name__ == "__main__":
    unittest.main()
