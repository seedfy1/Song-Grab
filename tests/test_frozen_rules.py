"""冻结校验：规则文件被改动就报错（对应「冻结规则文件」的要求）。"""
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "选片规则_冻结版.md"
SHA = ROOT / "docs" / "选片规则_冻结版.sha256"


def test_rules_doc_untouched():
    assert DOC.exists(), "规则文件不见了"
    assert SHA.exists(), "缺少 sha256 记录"
    want = SHA.read_text(encoding="utf-8").split()[0].strip()
    got = hashlib.sha256(DOC.read_bytes()).hexdigest()
    assert got == want, (
        f"规则文件被改动（{got[:12]} != {want[:12]}）。\n"
        "冻结版不允许直接改：要改规则请先和用户确认，再同时更新 .sha256 与修订记录。")


if __name__ == "__main__":
    try:
        test_rules_doc_untouched()
        print("  ok  test_rules_doc_untouched")
    except AssertionError as e:
        print(f"  FAIL {e}")
        sys.exit(1)
