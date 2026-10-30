import re
import unicodedata
from statistics import median
from uuid import NAMESPACE_URL, uuid5

from house_account.domain.ocr import Candidate, OcrBlock, OcrResult

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
TOTAL_START = re.compile(r"(?:割引前)?(?:小\s*計|合\s*計|総\s*計)(?=$|[\s/:：(（¥￥\d])")
QUANTITY = re.compile(r"\d+\s*(?:コ|個|点|本|枚)?\s*[×xX]\s*(?:単|@)?\s*[\d,.]+")
ADJUSTMENT = re.compile(r"[*\s]*(?:[値值]引額|[値值]引|割引額|[値值]引合計)[*\s]*")
AUXILIARY = re.compile(r"(?:商品代金|[値值]引合計|税率\s*\d+\s*%\s*[対对]象)")


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
    lines: list[list[int]] = []
    for index in sorted(range(len(blocks)), key=lambda i: (positions[i], blocks[i].left)):
        block = blocks[index]
        matching = [
            line
            for line in lines
            if abs(positions[index] - positions[line[0]])
            <= max(1.0, min(block.height, blocks[line[0]].height) * 0.45)
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
    end = next((index for index, text in enumerate(texts) if TOTAL_START.match(text)), len(lines))
    start = 0
    # 合計欄が明確な場合だけ購入明細の範囲を絞る。原文は範囲外も保持する。
    if end < len(lines):
        first_priced = next(
            (
                index
                for index, text in enumerate(texts[:end])
                if split_price(text)[1] and not METADATA.fullmatch(text)
            ),
            None,
        )
        if first_priced is not None:
            start = first_priced
            if QUANTITY.fullmatch(split_price(texts[first_priced])[0]):
                start = max(0, start - 1)
        else:
            end = len(lines)
    for line_index in range(start, end):
        indexes = lines[line_index]
        raw_text = raw_lines[line_index]
        text = texts[line_index]
        if not text or METADATA.fullmatch(text) or DATE_OR_PHONE.fullmatch(text):
            continue
        # 数字で終わる商品名の誤切り取りを避け、区切りか通貨表記を必要とする。
        name, has_price = split_price(text)
        label = name.strip("()（） ")
        if QUANTITY.fullmatch(label) or ADJUSTMENT.fullmatch(label) or AUXILIARY.fullmatch(label):
            continue
        if not name or not any(character.isalpha() for character in name):
            continue
        confidence = min(result.blocks[i].confidence for i in indexes)
        reasons = []
        if not has_price:
            reasons.append("price_not_found")
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
            )
        )
    return tuple(candidates)
