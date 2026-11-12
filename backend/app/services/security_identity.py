"""证券名称别名及代码边界校验，供本地与在线问答共用。"""
import re
from backend.app.models import FactRecord

STOCK_NAMES = {
    "贵州茅台": "600519", "茅台": "600519", "五粮液": "000858", "宁德时代": "300750",
    "贵州茅台酒股份有限公司": "600519", "宜宾五粮液股份有限公司": "000858",
    "招商银行": "600036", "中国平安": "601318", "美的集团": "000333", "长江电力": "600900",
}
STOCK_CODE = re.compile(r"(?<![0-9A-Za-z_])((?:60|68|00|30|43|83|87|88|92)\d{4})(?:\.(SH|SZ|BJ))?(?![0-9A-Za-z_.])", re.I)

def _market_suffix(code: str) -> str:
    return "SH" if code.startswith(("60", "68")) else "SZ" if code.startswith(("00", "30")) else "BJ"


def _stock_symbols(question: str, facts: list[FactRecord], *, stock_page: bool) -> tuple[str, ...]:
    names = dict(STOCK_NAMES)
    for fact in facts:
        match = STOCK_CODE.fullmatch(fact.entity_code or "")
        if match and match[2] and match[2].upper() == _market_suffix(match[1]) and len(fact.entity) >= 3:
            names.setdefault(fact.entity, match[1])
    pattern = re.compile("|".join(re.escape(name) for name in sorted(names, key=len, reverse=True)))
    hits = [(match.start(), names[match[0]]) for match in pattern.finditer(question)
            if not question[match.end():].startswith(("集团", "镇", "酒"))]
    for match in STOCK_CODE.finditer(question):
        code, suffix = match.groups()
        if suffix and suffix.upper() != _market_suffix(code):
            continue
        if suffix or stock_page or not code.startswith("00") or code in STOCK_NAMES.values():
            hits.append((match.start(), code))
    return tuple(dict.fromkeys(code for _, code in sorted(hits)))


