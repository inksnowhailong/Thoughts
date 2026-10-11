r"""二次识别输出的文字规整：单字母空格拆开的英文（「H A L O」）合并成一个词。

运行：cd /d F:\coding\guanjia && .venv\Scripts\python.exe tests\test_asr_text.py
"""
from guanjia.asr_text import merge_spaced_letters


def check(cond: bool, what: str) -> None:
    print(("PASS " if cond else "FAIL ") + what)
    if not cond:
        raise SystemExit(1)


def eq(got: str, want: str, what: str) -> None:
    check(got == want, f"{what}: {got!r}" + ("" if got == want else f" != {want!r}"))


eq(merge_spaced_letters("我的 H A L O 音响声音太小了"), "我的 HALO 音响声音太小了", "HALO 合并，两侧空格保留")
eq(merge_spaced_letters("人家帮我把这个 H A L 的这个音响"), "人家帮我把这个 HAL 的这个音响", "三个字母也合并")
eq(merge_spaced_letters("H A L O"), "HALO", "整句只有它")
eq(merge_spaced_letters("打开 Q Q 音乐"), "打开 QQ 音乐", "QQ")
eq(merge_spaced_letters("把 D 盘下载文件夹解压"), "把 D 盘下载文件夹解压", "单个字母不动（D 盘）")
eq(merge_spaced_letters("我的H A L O音响"), "我的HALO音响", "紧挨汉字（没有空格）也合并")
eq(merge_spaced_letters("TempoHub 打开了吗"), "TempoHub 打开了吗", "本来就连着的英文词不动")
eq(merge_spaced_letters("I love you"), "I love you", "正常英文句子不动")
eq(merge_spaced_letters("open A I now"), "open AI now", "A I 合并，旁边的完整单词不动")
eq(merge_spaced_letters("h a l o"), "halo", "小写也合并")
eq(merge_spaced_letters("H  A  L"), "H  A  L", "双空格不是 ASR 拆字的形态，不动")
eq(merge_spaced_letters("H A L 9000"), "HAL 9000", "后面跟数字")
eq(merge_spaced_letters(""), "", "空串")
eq(merge_spaced_letters("没有英文"), "没有英文", "纯中文不动")
eq(merge_spaced_letters("H A L O 和 Q Q"), "HALO 和 QQ", "一句里两处")
print("ALL PASS")
