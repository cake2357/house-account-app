from __future__ import annotations

import socket
import threading
import time
from collections.abc import Generator
from pathlib import Path

import pytest
import uvicorn
from playwright.sync_api import Page, expect, sync_playwright

from app import create_app
from house_account.config import Settings
from house_account.domain.ocr import OcrBlock, OcrResult


class FixedOcr:
    def __call__(
        self, image_path: Path, timeout_seconds: float, stopping: threading.Event
    ) -> OcrResult:
        return OcrResult(
            blocks=(
                OcrBlock(
                    text="りんご 198",
                    polygon=((10, 10), (220, 10), (220, 30), (10, 30)),
                    confidence=0.98,
                ),
            ),
            models={"recognition": "fixed"},
            versions={"paddleocr": "test"},
        )


@pytest.fixture
def app_url(tmp_path: Path) -> Generator[str]:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(
                Settings(data_dir=tmp_path / "data", model_dir=tmp_path / "models"), FixedOcr()
            ),
            host="127.0.0.1",
            port=port,
            log_level="error",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 8
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=3)
        pytest.fail("テスト用Webサーバーが起動しませんでした")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=8)


def select_tag(page: Page, item_index: int, tag_name: str) -> None:
    row = page.locator(".item-card").nth(item_index)
    picker = row.locator(".tag-picker")
    if not picker.evaluate("element => element.open"):
        row.locator(".tag-picker summary").click()
    search = row.locator(".tag-menu input[type=search]")
    search.fill(tag_name)
    row.get_by_role("button", name="この名前でタグを作成").click()
    option = row.locator(".tag-option").filter(has_text=tag_name).locator("input")
    expect(option).to_have_count(1)
    option.check()
    row.locator(".tag-picker summary").click()


def test_mobile_receipt_review_and_multiple_manual_tags(app_url: str, fixture_dir: Path) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page.set_default_timeout(5_000)
        page.goto(app_url)
        page.set_input_files("#receipt-file", str(fixture_dir / "synthetic_receipt.png"))
        page.get_by_role("button", name="登録する").click()
        page.wait_for_url("**/receipts/*", timeout=5_000)
        expect(page.locator("#candidate-list")).to_contain_text("りんご", timeout=5_000)
        page.get_by_role("button", name="候補を明細に追加").click()
        expect(page.locator(".item-card")).to_have_count(1)
        page.locator(".item-card").nth(0).get_by_label("商品名").fill("りんご（確認済み）")

        select_tag(page, 0, "食品")
        select_tag(page, 0, "常温")
        page.get_by_role("button", name="＋ 明細を追加").click()
        page.locator(".item-card").nth(1).get_by_label("商品名").fill("手入力した商品")
        select_tag(page, 1, "手入力")

        first_row = page.locator(".item-card").nth(0)
        expect(first_row.locator(".tag-chips")).to_contain_text("食品")
        expect(first_row.locator(".tag-chips")).to_contain_text("常温")
        expect(page.locator(".item-card").nth(1).locator(".tag-chips")).to_contain_text("手入力")

        page.get_by_role("button", name="確認して保存").click()
        expect(page.locator("#save-indicator")).to_have_text("保存済み")
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        page.reload()
        expect(page.locator(".item-card")).to_have_count(2)
        expect(page.locator(".item-card").nth(0).get_by_label("商品名")).to_have_value(
            "りんご（確認済み）"
        )
        expect(page.locator(".item-card").nth(0).locator(".tag-chips")).to_contain_text("食品")
        expect(page.locator(".item-card").nth(0).locator(".tag-chips")).to_contain_text("常温")
        page.get_by_role("button", name="常温を解除").click()
        page.get_by_role("button", name="確認して保存").click()
        expect(page.locator("#save-indicator")).to_have_text("保存済み")
        page.reload()
        expect(page.locator(".item-card").nth(0).locator(".tag-chips")).to_contain_text("食品")
        expect(page.locator(".item-card").nth(0).locator(".tag-chips")).not_to_contain_text("常温")
        browser.close()
