import re
import unicodedata
from statistics import median
from uuid import NAMESPACE_URL, uuid5

from house_account.domain.ocr import Candidate, OcrBlock, OcrResult, TotalAmountCandidate

PRICE = re.compile(
    r"(?P<price>(?:[¥￥*]\s*)?[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?"
    r"\s*(?:円)?\s*[*※軽]?\s*[)）]?)$"
)
METADATA = re.compile(
    r"(?:小\s*計|合\s*計|総\s*計|お買上(?:げ)?(?:計|合計)?|税込(?:合計)?|"
    r"(?:内|外)?(?:消費)?税(?:額|込|抜|対象|率)?(?:\s*\(?\d+\s*%\)?)?|"
    r"(?:\d+\s*%\s*)(?:対象(?:額)?|税込対象|税対象|税額)|"
    r"現金|クレジット(?:カード)?|電子マネー|お?預[りか]?り?|お?釣[り]?|"
    r"支払(?:い)?(?:金額)?|お?会計|ポイント(?:残高|利用|付与)?|"
    r"TEL|電話|登録番号|レジ|担当|領収書|レシート)"
    r"(?:\s*[:：]?\s*[\d¥￥円, .%()\-A-Za-z]+)?",
    re.IGNORECASE,
)
DATE_OR_PHONE = re.compile(
    r"(?:\d{2,4}[-/年]\d{1,2}[-/月]\d{1,2}日?.*|"
    r"\d{1,2}:\d{2}(?::\d{2})?|(?:TEL\s*[:：]?\s*)?0\d{1,4}[-ー]\d{1,4}[-ー]\d{3,4})",
    re.IGNORECASE,
)
HEADER = re.compile(r"(?:お知らせ|広告|キャンペーン).*")
TOTAL_LINE = re.compile(
    r"(?:割引前)?(?:小\s*計|合\s*計|総\s*計)(?:[/／]\s*\d+\s*(?:点|個)?)?\s*[:：]?\s*"
    + PRICE.pattern
)
TOTAL_AMOUNT_LINE = re.compile(
    r"(?:合\s*計|総\s*計|総額|お買上(?:げ)?(?:合計|計)|お買い上げ(?:合計|計)|"
    r"税込合計|ご請求額|請求金額|お?支払(?:い)?(?:合計|金額))"
    r"(?:[/／]\s*\d+\s*(?:点|個)?)?\s*[:：]?\s*" + PRICE.pattern,
    re.IGNORECASE,
)
QUANTITY = re.compile(r"\d+\s*(?:コ|個|点|本|枚)?\s*[×xX]\s*(?:単|@)?\s*[\d,.]+")
ADJUSTMENT = re.compile(r"[*\s]*(?:[値值]引額|[値值]引|割引額|[値值]引合計)[*\s]*")
AUXILIARY = re.compile(r"(?:商品代金|[値值]引合計|税率\s*\d+\s*%\s*[対对]象)")
SUPPLEMENTAL_METADATA = re.compile(
    r"(?:.*(?:事業者番号|登録番号|営業時間|住所|スキャン(?:No|番号)|"
    r"(?:ス|レジ|レト|キレ)\s*N?o?\s*\d+).*)|"
    r"(?:.*店$)|"
    r"(?:.*(?:会員で現金|本体価格の|\d+\s*%相当|食料品は$).*)|"
    r"(?i:.*(?:everyday low price|low price|high quality).*)|"
    r"(?i:(?:ok|low|price))|"
    r"(?:^(?:東京都|北海道|大阪府|神奈川県|[一-龯]+[市区町村]).*(?:\d|丁目|番地|号).*)|"
    r"(?:.*電話\s*[:：]?\s*0\d{1,4}[-ー]\d{1,4}[-ー]\d{3,4}.*)|"
    r"(?:(?:小\s*計|合\s*計).*税.*|消費税.*|税額.*)"
)


def split_price(text: str) -> tuple[str, bool]:
    price = PRICE.search(text)
    has_price = bool(
        price
        and price.start() > 0
        and (
            text[price.start() - 1].isspace()
            or text[price.start()] in "¥￥*"
            or "円" in price.group()
        )
    )
    return (text[: price.start()].strip(), True) if has_price and price else (text, False)


def price_yen(text: str) -> int | None:
    price = PRICE.search(text)
    if price is None:
        return None
    value = price.group("price").strip()
    value = re.sub(r"^[¥￥*\s]+|[円*※軽\s)）]+$", "", value)
    value = value.replace(",", "")
    if not re.fullmatch(r"[+-]?\d+", value):
        return None
    try:
        return int(value)
    except ValueError:
        return None


def group_lines(blocks: tuple[OcrBlock, ...]) -> list[list[int]]:
    """縦位置が近いブロックを結び、行内は左から右に並べる。"""
    slopes = []
    for block in blocks:
        top_left, top_right, _, _ = block.polygon
        width = top_right[0] - top_left[0]
        if width > block.height * 2:
            slope = (top_right[1] - top_left[1]) / width
            if abs(slope) <= 0.25:
                slopes.append(slope)
    page_slope = median(slopes) if slopes else 0.0
    positions = [block.center_y - page_slope * block.center_x for block in blocks]
    adjusted_heights = [
        max(y - page_slope * x for x, y in block.polygon)
        - min(y - page_slope * x for x, y in block.polygon)
        for block in blocks
    ]
    lines: list[list[int]] = []
    for index in sorted(range(len(blocks)), key=lambda i: (positions[i], blocks[i].left)):
        block = blocks[index]
        matching = [
            line
            for line in lines
            if abs(positions[index] - positions[line[0]])
            <= max(1.0, min(adjusted_heights[index], adjusted_heights[line[0]]) * 0.45)
        ]
        if matching:
            min(matching, key=lambda line: abs(positions[index] - positions[line[0]])).append(index)
        else:
            lines.append([index])
    return [sorted(line, key=lambda i: blocks[i].left) for line in lines]


def extract_candidates(result: OcrResult) -> tuple[Candidate, ...]:
    candidates: list[Candidate] = []
    lines = group_lines(result.blocks)
    raw_lines = [" ".join(result.blocks[i].text.strip() for i in line).strip() for line in lines]
    texts = [unicodedata.normalize("NFKC", text) for text in raw_lines]
    end = next(
        (index for index, text in enumerate(texts) if TOTAL_LINE.fullmatch(text)), len(lines)
    )
    for line_index in range(end):
        indexes = lines[line_index]
        raw_text = raw_lines[line_index]
        text = texts[line_index]
        if (
            not text
            or METADATA.fullmatch(text)
            or SUPPLEMENTAL_METADATA.fullmatch(text)
            or DATE_OR_PHONE.fullmatch(text)
            or HEADER.fullmatch(text)
            or PRICE.fullmatch(text)
        ):
            continue
        # 数字で終わる商品名の誤切り取りを避け、区切りか通貨表記を必要とする。
        name, has_price = split_price(text)
        amount_yen = price_yen(text) if has_price else None
        label = name.strip("()（） ")
        if QUANTITY.fullmatch(label) or ADJUSTMENT.fullmatch(label) or AUXILIARY.fullmatch(label):
            continue
        if not name or not any(character.isalpha() for character in name):
            continue
        confidence = min(result.blocks[i].confidence for i in indexes)
        reasons = []
        if not has_price:
            reasons.append("price_not_found")
        elif amount_yen is None:
            reasons.append("amount_not_integer")
        if confidence < 0.80:
            reasons.append("low_confidence")
        candidate_id = str(uuid5(NAMESPACE_URL, f"{indexes}:{raw_text}"))
        candidates.append(
            Candidate(
                id=candidate_id,
                name=name,
                raw_text=raw_text,
                block_indexes=tuple(indexes),
                confidence=confidence,
                needs_review=bool(reasons),
                review_reasons=tuple(reasons),
                amount_yen=amount_yen,
                amount_source_text=raw_text if amount_yen is not None else None,
                amount_source_polygons=(
                    tuple(result.blocks[i].polygon for i in indexes)
                    if amount_yen is not None
                    else ()
                ),
            )
        )
    return tuple(candidates)


def extract_total_amount_candidates(result: OcrResult) -> tuple[TotalAmountCandidate, ...]:
    lines = group_lines(result.blocks)
    candidates: list[TotalAmountCandidate] = []
    for indexes in lines:
        raw_text = " ".join(result.blocks[index].text.strip() for index in indexes).strip()
        text = unicodedata.normalize("NFKC", raw_text)
        if not TOTAL_AMOUNT_LINE.fullmatch(text):
            continue
        amount_yen = price_yen(text)
        if amount_yen is None or amount_yen < 0:
            continue
        candidate_id = str(uuid5(NAMESPACE_URL, f"total:{indexes}:{raw_text}"))
        candidates.append(
            TotalAmountCandidate(
                id=candidate_id,
                total_amount_yen=amount_yen,
                raw_text=raw_text,
                block_indexes=tuple(indexes),
                source_polygons=tuple(result.blocks[index].polygon for index in indexes),
            )
        )
    return tuple(candidates)
