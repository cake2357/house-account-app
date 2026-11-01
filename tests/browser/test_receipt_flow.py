from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Generator
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import uvicorn
from playwright.sync_api import Dialog, Page, Route, expect, sync_playwright

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


def test_long_upload_filename_is_truncated_on_mobile(app_url: str) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page.goto(app_url)
        filename = "receipt-" + "x" * 120 + ".png"
        page.locator("#receipt-file").set_input_files(
            {"name": filename, "mimeType": "image/png", "buffer": b"image"}
        )

        filename_label = page.locator(".file-picker strong")
        expect(filename_label).to_have_text(filename)
        dimensions = filename_label.evaluate(
            """element => ({
              clientWidth: element.clientWidth,
              scrollWidth: element.scrollWidth,
              textOverflow: getComputedStyle(element).textOverflow,
            })"""
        )
        assert dimensions["clientWidth"] < dimensions["scrollWidth"]
        assert dimensions["textOverflow"] == "ellipsis"
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        browser.close()


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
        first_row = page.locator(".item-card").nth(0)
        first_row.locator(".tag-picker summary").click()
        menu = first_row.locator(".tag-menu")
        expect(menu).to_be_visible()
        bounds = first_row.evaluate(
            """row => {
              const card = row.getBoundingClientRect();
              const menu = row.querySelector(".tag-menu").getBoundingClientRect();
              return { cardLeft: card.left, cardRight: card.right, menuLeft: menu.left, menuRight: menu.right };
            }"""
        )
        assert bounds["menuLeft"] >= bounds["cardLeft"]
        assert bounds["menuRight"] <= bounds["cardRight"]
        first_row.locator(".tag-picker summary").click()
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


def test_receipt_editing_waits_for_initial_detail_load(app_url: str, fixture_dir: Path) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.set_default_timeout(5_000)
        delayed_detail_requests: list[Route] = []

        def hold_receipt_detail(route: Route) -> None:
            path = urlsplit(route.request.url).path
            is_receipt_detail = path.startswith("/api/receipts/") and path.count("/") == 3
            if route.request.method == "GET" and is_receipt_detail:
                delayed_detail_requests.append(route)
                return
            route.continue_()

        page.route("**/api/receipts/*", hold_receipt_detail)
        page.goto(app_url)
        page.set_input_files("#receipt-file", str(fixture_dir / "synthetic_receipt.png"))
        with page.expect_request(
            lambda request: (
                request.method == "GET"
                and urlsplit(request.url).path.startswith("/api/receipts/")
                and urlsplit(request.url).path.count("/") == 3
            )
        ):
            page.get_by_role("button", name="登録する").click()
        page.wait_for_url("**/receipts/*", timeout=5_000)
        page.wait_for_timeout(25)

        assert delayed_detail_requests
        expect(page.locator("#add-item")).to_be_disabled()
        expect(page.locator("#save-draft")).to_be_disabled()
        expect(page.locator("#save-confirmed")).to_be_disabled()
        page.locator("#add-item").evaluate(
            "button => button.dispatchEvent(new MouseEvent('click', { bubbles: true }))"
        )
        expect(page.locator(".item-card")).to_have_count(0)
        expect(page.locator("#save-indicator")).to_have_text("未保存")
        assert not page.evaluate(
            """() => {
              const event = new Event("beforeunload", { cancelable: true });
              window.dispatchEvent(event);
              return event.defaultPrevented;
            }"""
        )

        delayed_detail_requests[0].continue_()
        expect(page.locator("#add-item")).to_be_enabled()
        page.get_by_role("button", name="＋ 明細を追加").click()
        expect(page.locator(".item-card")).to_have_count(1)
        expect(page.locator("#save-indicator")).to_have_text("未保存")
        browser.close()


def test_saving_disables_candidate_addition_and_warns_before_unload(
    app_url: str, fixture_dir: Path
) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.set_default_timeout(5_000)
        page.goto(app_url)
        page.set_input_files("#receipt-file", str(fixture_dir / "synthetic_receipt.png"))
        page.get_by_role("button", name="登録する").click()
        page.wait_for_url("**/receipts/*", timeout=5_000)
        add_candidates = page.get_by_role("button", name="候補を明細に追加")
        expect(add_candidates).to_be_enabled(timeout=5_000)
        add_candidates.click()
        expect(page.locator(".item-card")).to_have_count(1)

        page.evaluate(
            """() => {
              const nativeFetch = window.fetch.bind(window);
              window.fetch = async (input, options = {}) => {
                const url = typeof input === "string" ? input : input.url;
                if (url.endsWith("/items") && options.method === "PUT") {
                  return new Promise((resolve) => {
                    window.__releaseSave = (body) => resolve(new Response(JSON.stringify(body), {
                      status: 200,
                      headers: { "Content-Type": "application/json" },
                    }));
                  });
                }
                return nativeFetch(input, options);
              };
            }"""
        )
        page.get_by_role("button", name="下書き保存").click()
        expect(page.locator("#save-indicator")).to_have_text("保存中")
        expect(add_candidates).to_be_disabled()
        page.evaluate(
            """() => document.querySelector("#add-candidates").dispatchEvent(
              new MouseEvent("click", { bubbles: true })
            )"""
        )
        expect(page.locator(".item-card")).to_have_count(1)
        assert page.evaluate(
            """() => {
              const event = new Event("beforeunload", { cancelable: true });
              window.dispatchEvent(event);
              return event.defaultPrevented;
            }"""
        )
        page.evaluate(
            """() => window.__releaseSave({
              revision: 2,
              status: "draft",
              created_at: "2026-01-01T00:00:00+00:00",
              items: [{ id: "saved-item" }],
            })"""
        )
        expect(page.locator("#save-indicator")).to_have_text("保存済み")
        browser.close()


def test_save_network_failure_restores_failure_state_without_field_errors(
    app_url: str, fixture_dir: Path
) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.set_default_timeout(5_000)
        page.goto(app_url)
        page.set_input_files("#receipt-file", str(fixture_dir / "synthetic_receipt.png"))
        page.get_by_role("button", name="登録する").click()
        page.wait_for_url("**/receipts/*", timeout=5_000)
        page.get_by_role("button", name="＋ 明細を追加").click()
        page.get_by_label("商品名").fill("通信失敗の確認")
        page.route("**/api/receipts/*/items", lambda route: route.abort())

        page.get_by_role("button", name="下書き保存").click()

        expect(page.locator("#save-indicator")).to_have_text("保存できませんでした")
        expect(page.locator("#save-error")).not_to_be_empty()
        expect(page.get_by_role("button", name="下書き保存")).to_be_enabled()
        browser.close()


def test_tag_search_ignores_responses_for_older_queries(app_url: str, fixture_dir: Path) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.set_default_timeout(5_000)
        page.goto(app_url)
        page.set_input_files("#receipt-file", str(fixture_dir / "synthetic_receipt.png"))
        page.get_by_role("button", name="登録する").click()
        page.wait_for_url("**/receipts/*", timeout=5_000)
        page.get_by_role("button", name="＋ 明細を追加").click()

        row = page.locator(".item-card").first
        row.locator(".tag-picker summary").click()
        page.evaluate(
            """() => {
              const nativeFetch = window.fetch.bind(window);
              window.fetch = (input, options) => {
                const url = new URL(
                  typeof input === "string" ? input : input.url,
                  window.location.origin,
                );
                const query = url.searchParams.get("q");
                if (url.pathname === "/api/tags" && ["A", "B"].includes(query)) {
                  return new Promise((resolve) => {
                    window[`__releaseTagSearch${query}`] = () => resolve(new Response(
                      JSON.stringify({ items: [{ id: query, name: `タグ${query}` }] }),
                      { status: 200, headers: { "Content-Type": "application/json" } },
                    ));
                  });
                }
                return nativeFetch(input, options);
              };
            }"""
        )

        search = row.get_by_label("タグを検索")
        search.fill("A")
        page.wait_for_function("() => typeof window.__releaseTagSearchA === 'function'")
        search.fill("B")
        page.wait_for_function("() => typeof window.__releaseTagSearchB === 'function'")

        page.evaluate("() => window.__releaseTagSearchB()")
        expect(row.locator(".tag-option")).to_contain_text("タグB")
        page.evaluate("() => window.__releaseTagSearchA()")
        expect(row.locator(".tag-option")).to_contain_text("タグB")
        expect(row.locator(".tag-option")).not_to_contain_text("タグA")
        browser.close()


def test_tag_search_results_remain_disabled_while_saving(app_url: str, fixture_dir: Path) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.set_default_timeout(5_000)
        page.goto(app_url)
        page.set_input_files("#receipt-file", str(fixture_dir / "synthetic_receipt.png"))
        page.get_by_role("button", name="登録する").click()
        page.wait_for_url("**/receipts/*", timeout=5_000)
        expect(page.locator("#candidate-list")).to_contain_text("りんご", timeout=5_000)
        page.get_by_role("button", name="候補を明細に追加").click()
        select_tag(page, 0, "食品")

        page.evaluate(
            """() => {
              const nativeFetch = window.fetch.bind(window);
              window.fetch = async (input, options = {}) => {
                const url = new URL(
                  typeof input === "string" ? input : input.url,
                  window.location.origin,
                );
                if (url.pathname === "/api/tags" && url.searchParams.get("q") === "食品") {
                  return new Promise((resolve) => {
                    window.__releaseTagSearch = () => resolve(nativeFetch(input, options));
                  });
                }
                if (url.pathname.endsWith("/items") && options.method === "PUT") {
                  return new Promise((resolve) => {
                    window.__releaseSave = (body) => resolve(new Response(JSON.stringify(body), {
                      status: 200,
                      headers: { "Content-Type": "application/json" },
                    }));
                  });
                }
                return nativeFetch(input, options);
              };
            }"""
        )

        row = page.locator(".item-card").first
        picker = row.locator(".tag-picker")
        if not picker.evaluate("element => element.open"):
            row.locator(".tag-picker summary").click()
        row.get_by_label("タグを検索").fill("食品")
        page.wait_for_function("() => typeof window.__releaseTagSearch === 'function'")
        page.get_by_role("button", name="下書き保存").click()
        expect(page.locator("#save-indicator")).to_have_text("保存中")

        page.evaluate("() => window.__releaseTagSearch()")
        tag_checkbox = row.locator(".tag-option input").first
        remove_tag = row.get_by_role("button", name="食品を解除")
        expect(tag_checkbox).to_be_disabled()
        expect(remove_tag).to_be_disabled()
        expect(page.locator("#save-indicator")).to_have_text("保存中")

        page.evaluate(
            """() => window.__releaseSave({
              revision: 2,
              status: "draft",
              created_at: "2026-01-01T00:00:00+00:00",
              items: [{ id: "saved-item" }],
            })"""
        )
        expect(page.locator("#save-indicator")).to_have_text("保存済み")
        expect(tag_checkbox).to_be_enabled()
        browser.close()


def test_joining_existing_ocr_run_starts_polling(app_url: str, fixture_dir: Path) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.set_default_timeout(5_000)
        page.goto(app_url)
        page.set_input_files("#receipt-file", str(fixture_dir / "synthetic_receipt.png"))
        page.get_by_role("button", name="登録する").click()
        page.wait_for_url("**/receipts/*", timeout=5_000)
        expect(page.locator("#candidate-list")).to_contain_text("りんご", timeout=5_000)
        expect(page.locator("#ocr-state")).to_have_text("読み取り完了", timeout=5_000)

        poll_count = 0

        def return_existing_run(route: Route) -> None:
            nonlocal poll_count
            poll_count += 1
            run = {
                "id": "shared-run",
                "status": "queued" if poll_count == 1 else "succeeded",
                "candidates": []
                if poll_count == 1
                else [
                    {
                        "id": "shared-candidate",
                        "name": "別画面で開始された候補",
                        "needs_review": False,
                    }
                ],
                "raw": None,
                "error_message": None,
            }
            route.fulfill(
                status=200,
                body=json.dumps(run, ensure_ascii=False),
                content_type="application/json",
            )

        page.route(
            "**/api/receipts/*/ocr-runs",
            lambda route: route.fulfill(
                status=202,
                body=json.dumps({"created": False, "run_id": "shared-run"}),
                content_type="application/json",
            ),
        )
        page.route("**/api/ocr-runs/shared-run", return_existing_run)
        page.locator("details.extra-actions > summary").click()
        page.get_by_role("button", name="OCRを再実行").click()

        expect(page.locator("#candidate-list")).to_contain_text(
            "別画面で開始された候補", timeout=7_000
        )
        assert poll_count >= 2
        browser.close()


def test_polling_serializes_requests_for_the_same_ocr_run(app_url: str, fixture_dir: Path) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.set_default_timeout(5_000)

        def expose_running_run(route: Route) -> None:
            path = urlsplit(route.request.url).path
            is_receipt_detail = path.startswith("/api/receipts/") and path.count("/") == 3
            if route.request.method == "GET" and is_receipt_detail:
                response = route.fetch()
                detail = response.json()
                detail["latest_run"] = {
                    "id": "serial-run",
                    "status": "running",
                    "candidates": [],
                    "raw": None,
                    "error_message": None,
                }
                detail["last_successful_run"] = None
                route.fulfill(response=response, json=detail)
                return
            route.continue_()

        page.route("**/api/receipts/*", expose_running_run)
        page.add_init_script(
            """(() => {
              const nativeFetch = window.fetch.bind(window);
              window.__serialPollCount = 0;
              window.fetch = async (input, options = {}) => {
                const url = new URL(
                  typeof input === "string" ? input : input.url,
                  window.location.origin,
                );
                if (url.pathname === "/api/ocr-runs/serial-run") {
                  window.__serialPollCount += 1;
                  if (window.__serialPollCount === 1) {
                    return new Promise((resolve) => {
                      window.__releaseFirstSerialPoll = () => resolve(new Response(JSON.stringify({
                        id: "serial-run",
                        status: "running",
                        candidates: [],
                        raw: null,
                        error_message: null,
                      }), { status: 200, headers: { "Content-Type": "application/json" } }));
                    });
                  }
                  return new Response(JSON.stringify({
                    id: "serial-run",
                    status: "succeeded",
                    candidates: [{ id: "serial-candidate", name: "直列後の候補", needs_review: false }],
                    raw: null,
                    error_message: null,
                  }), { status: 200, headers: { "Content-Type": "application/json" } });
                }
                return nativeFetch(input, options);
              };
            })();"""
        )

        page.goto(app_url)
        page.set_input_files("#receipt-file", str(fixture_dir / "synthetic_receipt.png"))
        page.get_by_role("button", name="登録する").click()
        page.wait_for_url("**/receipts/*", timeout=5_000)
        expect(page.locator("#ocr-state")).to_have_text("読み取り中")
        page.wait_for_function("() => window.__serialPollCount === 1", timeout=4_000)
        page.wait_for_timeout(2_100)

        assert page.evaluate("() => window.__serialPollCount") == 1
        page.evaluate("() => window.__releaseFirstSerialPoll()")

        expect(page.locator("#candidate-list")).to_contain_text("直列後の候補", timeout=5_000)
        expect(page.locator("#ocr-state")).to_have_text("読み取り完了")
        assert page.evaluate("() => window.__serialPollCount") == 2
        browser.close()


def test_stale_ocr_poll_response_does_not_replace_newer_run(
    app_url: str, fixture_dir: Path
) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.set_default_timeout(5_000)

        def expose_running_run(route: Route) -> None:
            path = urlsplit(route.request.url).path
            is_receipt_detail = path.startswith("/api/receipts/") and path.count("/") == 3
            if route.request.method == "GET" and is_receipt_detail:
                response = route.fetch()
                detail = response.json()
                detail["latest_run"] = {
                    "id": "run-A",
                    "status": "running",
                    "candidates": [],
                    "raw": None,
                    "error_message": None,
                }
                route.fulfill(response=response, json=detail)
                return
            route.continue_()

        page.route("**/api/receipts/*", expose_running_run)
        page.add_init_script(
            """(() => {
              const nativeFetch = window.fetch.bind(window);
              window.__runBPollCount = 0;
              window.__runAStarted = false;
              window.fetch = async (input, options = {}) => {
                const url = new URL(
                  typeof input === "string" ? input : input.url,
                  window.location.origin,
                );
                if (url.pathname === "/api/ocr-runs/run-A") {
                  window.__runAStarted = true;
                  return new Promise((resolve) => {
                    window.__releaseRunA = () => {
                      resolve(new Response(JSON.stringify({
                        id: "run-A",
                        status: "succeeded",
                        candidates: [{ id: "candidate-A", name: "古い実行A候補", needs_review: false }],
                        raw: null,
                        error_message: null,
                      }), { status: 200, headers: { "Content-Type": "application/json" } }));
                      window.__runAResponseReleased = true;
                    };
                  });
                }
                if (url.pathname.endsWith("/ocr-runs") && options.method === "POST") {
                  return new Response(JSON.stringify({ run_id: "run-B", created: true }), {
                    status: 202,
                    headers: { "Content-Type": "application/json" },
                  });
                }
                if (url.pathname === "/api/ocr-runs/run-B") {
                  window.__runBPollCount += 1;
                  const succeeded = window.__runBPollCount > 1;
                  return new Response(JSON.stringify({
                    id: "run-B",
                    status: succeeded ? "succeeded" : "running",
                    candidates: succeeded
                      ? [{ id: "candidate-B", name: "新しい実行B候補", needs_review: false }]
                      : [],
                    raw: null,
                    error_message: null,
                  }), { status: 200, headers: { "Content-Type": "application/json" } });
                }
                return nativeFetch(input, options);
              };
            })();"""
        )

        page.goto(app_url)
        page.set_input_files("#receipt-file", str(fixture_dir / "synthetic_receipt.png"))
        page.get_by_role("button", name="登録する").click()
        page.wait_for_url("**/receipts/*", timeout=5_000)
        expect(page.locator("#ocr-state")).to_have_text("読み取り中")
        page.wait_for_function("() => window.__runAStarted === true", timeout=4_000)

        page.locator("details.extra-actions > summary").click()
        page.get_by_role("button", name="OCRを再実行").click()
        page.wait_for_function("() => window.__runBPollCount === 1")
        page.evaluate("() => window.__releaseRunA()")
        page.wait_for_function("() => window.__runAResponseReleased === true")
        page.wait_for_timeout(50)

        expect(page.locator("#ocr-state")).to_have_text("読み取り中")
        expect(page.locator("#candidate-list")).not_to_contain_text("古い実行A候補")
        expect(page.locator("#candidate-list")).to_contain_text("新しい実行B候補", timeout=5_000)
        browser.close()


def test_rerun_stays_disabled_during_requests_and_discards_old_poll(
    app_url: str, fixture_dir: Path
) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.set_default_timeout(5_000)

        def expose_running_run(route: Route) -> None:
            path = urlsplit(route.request.url).path
            is_receipt_detail = path.startswith("/api/receipts/") and path.count("/") == 3
            if route.request.method == "GET" and is_receipt_detail:
                response = route.fetch()
                detail = response.json()
                detail["latest_run"] = {
                    "id": "run-A",
                    "status": "running",
                    "candidates": [],
                    "raw": None,
                    "error_message": None,
                }
                detail["last_successful_run"] = None
                route.fulfill(response=response, json=detail)
                return
            route.continue_()

        page.route("**/api/receipts/*", expose_running_run)
        page.add_init_script(
            """(() => {
              const nativeFetch = window.fetch.bind(window);
              window.__runAPollCount = 0;
              window.__rerunPostCount = 0;
              window.fetch = async (input, options = {}) => {
                const url = new URL(
                  typeof input === "string" ? input : input.url,
                  window.location.origin,
                );
                if (url.pathname === "/api/ocr-runs/run-A") {
                  window.__runAPollCount += 1;
                  if (window.__runAPollCount === 1) {
                    return new Promise((resolve) => {
                      window.__releaseRunA = () => {
                        resolve(new Response(JSON.stringify({
                          id: "run-A",
                          status: "succeeded",
                          candidates: [{ id: "candidate-A", name: "古い実行A候補", needs_review: false }],
                          raw: null,
                          error_message: null,
                        }), { status: 200, headers: { "Content-Type": "application/json" } }));
                        window.__oldRunAReleased = true;
                      };
                    });
                  }
                  return new Response(JSON.stringify({
                    id: "run-A",
                    status: "running",
                    candidates: [],
                    raw: null,
                    error_message: null,
                  }), { status: 200, headers: { "Content-Type": "application/json" } });
                }
                if (url.pathname.endsWith("/ocr-runs") && options.method === "POST") {
                  window.__rerunPostCount += 1;
                  return new Response(JSON.stringify({ run_id: "run-B", created: true }), {
                    status: 202,
                    headers: { "Content-Type": "application/json" },
                  });
                }
                if (url.pathname === "/api/ocr-runs/run-B") {
                  return new Promise((resolve) => {
                    window.__releaseRunB = () => resolve(new Response(JSON.stringify({
                      id: "run-B",
                      status: "succeeded",
                      candidates: [{ id: "candidate-B", name: "新しい実行B候補", needs_review: false }],
                      raw: null,
                      error_message: null,
                    }), { status: 200, headers: { "Content-Type": "application/json" } }));
                    window.__runBStarted = true;
                  });
                }
                return nativeFetch(input, options);
              };
            })();"""
        )

        page.goto(app_url)
        page.set_input_files("#receipt-file", str(fixture_dir / "synthetic_receipt.png"))
        page.get_by_role("button", name="登録する").click()
        page.wait_for_url("**/receipts/*", timeout=5_000)
        expect(page.locator("#ocr-state")).to_have_text("読み取り中")
        page.wait_for_function("() => window.__runAPollCount === 1", timeout=4_000)

        page.locator("details.extra-actions > summary").click()
        rerun_button = page.get_by_role("button", name="OCRを再実行")
        rerun_button.click()
        page.wait_for_function("() => window.__runBStarted === true")
        expect(rerun_button).to_be_disabled()

        page.get_by_role("button", name="＋ 明細を追加").click()
        row = page.locator(".item-card").first
        row.get_by_label("商品名").fill("再実行中に保存する明細")
        with page.expect_response(
            lambda response: urlsplit(response.url).path == "/api/tags" and response.request.method == "GET"
        ):
            row.locator(".tag-picker summary").click()
        expect(rerun_button).to_be_disabled()

        rerun_button.evaluate(
            "button => button.dispatchEvent(new MouseEvent('click', { bubbles: true }))"
        )
        assert page.evaluate("() => window.__rerunPostCount") == 1
        page.get_by_role("button", name="下書き保存").click()
        expect(page.locator("#save-indicator")).to_have_text("保存済み")
        expect(rerun_button).to_be_disabled()

        page.evaluate("() => window.__releaseRunA()")
        page.wait_for_function("() => window.__oldRunAReleased === true")
        expect(page.locator("#ocr-state")).to_have_text("読み取り中")
        expect(page.locator("#candidate-list")).not_to_contain_text("古い実行A候補")

        page.evaluate("() => window.__releaseRunB()")
        expect(page.locator("#candidate-list")).to_contain_text("新しい実行B候補")
        expect(rerun_button).to_be_enabled()
        browser.close()


def test_deleting_dirty_receipt_clears_beforeunload_warning(
    app_url: str, fixture_dir: Path
) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.set_default_timeout(5_000)
        dialog_types: list[str] = []

        def handle_dialog(dialog: Dialog) -> None:
            dialog_types.append(dialog.type)
            if dialog.type == "confirm":
                dialog.accept()
            else:
                dialog.dismiss()

        page.on("dialog", handle_dialog)
        page.goto(app_url)
        page.set_input_files("#receipt-file", str(fixture_dir / "synthetic_receipt.png"))
        page.get_by_role("button", name="登録する").click()
        page.wait_for_url("**/receipts/*", timeout=5_000)
        expect(page.locator("#candidate-list")).to_contain_text("りんご", timeout=5_000)
        page.get_by_role("button", name="候補を明細に追加").click()
        expect(page.locator("#save-indicator")).to_have_text("未保存")

        page.locator("details.extra-actions > summary").click()
        page.get_by_role("button", name="レシートを削除").click()
        page.wait_for_url(f"{app_url}/", timeout=5_000)

        assert dialog_types == ["confirm"]
        browser.close()


def test_immediately_completed_rerun_becomes_the_addable_candidate_run(
    app_url: str, fixture_dir: Path
) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.set_default_timeout(5_000)

        def hide_existing_runs(route: Route) -> None:
            path = urlsplit(route.request.url).path
            is_receipt_detail = path.startswith("/api/receipts/") and path.count("/") == 3
            if route.request.method == "GET" and is_receipt_detail:
                response = route.fetch()
                detail = response.json()
                detail["latest_run"] = None
                detail["last_successful_run"] = None
                route.fulfill(response=response, json=detail)
                return
            route.continue_()

        page.route("**/api/receipts/*", hide_existing_runs)
        page.route(
            "**/api/receipts/*/ocr-runs",
            lambda route: route.fulfill(
                status=202,
                body=json.dumps({"created": True, "run_id": "instant-run"}),
                content_type="application/json",
            ),
        )
        page.route(
            "**/api/ocr-runs/instant-run",
            lambda route: route.fulfill(
                status=200,
                body=json.dumps(
                    {
                        "id": "instant-run",
                        "status": "succeeded",
                        "candidates": [
                            {
                                "id": "instant-candidate",
                                "name": "即時完了候補",
                                "needs_review": False,
                            }
                        ],
                        "raw": None,
                        "error_message": None,
                    },
                    ensure_ascii=False,
                ),
                content_type="application/json",
            ),
        )

        page.goto(app_url)
        page.set_input_files("#receipt-file", str(fixture_dir / "synthetic_receipt.png"))
        page.get_by_role("button", name="登録する").click()
        page.wait_for_url("**/receipts/*", timeout=5_000)
        expect(page.locator("#ocr-state")).to_have_text("未実行")
        page.locator("details.extra-actions > summary").click()
        page.get_by_role("button", name="OCRを再実行").click()

        expect(page.locator("#candidate-list")).to_contain_text("即時完了候補")
        add_candidates = page.get_by_role("button", name="候補を明細に追加")
        expect(add_candidates).to_be_enabled()
        add_candidates.click()
        expect(page.locator(".item-card")).to_have_count(1)
        expect(page.locator(".item-card").first.get_by_label("商品名")).to_have_value(
            "即時完了候補"
        )
        browser.close()


def test_old_poll_response_cannot_replace_completed_same_id_rerun(
    app_url: str, fixture_dir: Path
) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.set_default_timeout(5_000)

        def expose_running_run(route: Route) -> None:
            path = urlsplit(route.request.url).path
            is_receipt_detail = path.startswith("/api/receipts/") and path.count("/") == 3
            if route.request.method == "GET" and is_receipt_detail:
                response = route.fetch()
                detail = response.json()
                detail["latest_run"] = {
                    "id": "same-run",
                    "status": "running",
                    "candidates": [],
                    "raw": None,
                    "error_message": None,
                }
                detail["last_successful_run"] = None
                route.fulfill(response=response, json=detail)
                return
            route.continue_()

        page.route("**/api/receipts/*", expose_running_run)
        page.add_init_script(
            """(() => {
              const nativeFetch = window.fetch.bind(window);
              window.__sameRunPollCount = 0;
              window.fetch = async (input, options = {}) => {
                const url = new URL(
                  typeof input === "string" ? input : input.url,
                  window.location.origin,
                );
                if (url.pathname === "/api/ocr-runs/same-run") {
                  window.__sameRunPollCount += 1;
                  if (window.__sameRunPollCount === 1) {
                    return new Promise((resolve) => {
                      window.__releaseOldSameRun = () => {
                        resolve(new Response(JSON.stringify({
                          id: "same-run",
                          status: "running",
                          candidates: [],
                          raw: null,
                          error_message: null,
                        }), { status: 200, headers: { "Content-Type": "application/json" } }));
                        window.__oldSameRunReleased = true;
                      };
                    });
                  }
                  return new Response(JSON.stringify({
                    id: "same-run",
                    status: "succeeded",
                    candidates: [{ id: "same-candidate", name: "完了済み候補", needs_review: false }],
                    raw: null,
                    error_message: null,
                  }), { status: 200, headers: { "Content-Type": "application/json" } });
                }
                if (url.pathname.endsWith("/ocr-runs") && options.method === "POST") {
                  return new Response(JSON.stringify({ run_id: "same-run", created: false }), {
                    status: 202,
                    headers: { "Content-Type": "application/json" },
                  });
                }
                return nativeFetch(input, options);
              };
            })();"""
        )

        page.goto(app_url)
        page.set_input_files("#receipt-file", str(fixture_dir / "synthetic_receipt.png"))
        page.get_by_role("button", name="登録する").click()
        page.wait_for_url("**/receipts/*", timeout=5_000)
        page.wait_for_function("() => window.__sameRunPollCount === 1", timeout=4_000)

        page.locator("details.extra-actions > summary").click()
        page.get_by_role("button", name="OCRを再実行").click()
        expect(page.locator("#ocr-state")).to_have_text("読み取り完了")
        page.evaluate("() => window.__releaseOldSameRun()")
        page.wait_for_function("() => window.__oldSameRunReleased === true")

        expect(page.locator("#ocr-state")).to_have_text("読み取り完了")
        expect(page.locator("#candidate-list")).to_contain_text("完了済み候補")
        browser.close()


def test_pagination_disables_navigation_until_requested_page_is_rendered(
    app_url: str,
) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.set_default_timeout(5_000)
        page.add_init_script(
            """(() => {
              const nativeFetch = window.fetch.bind(window);
              window.__receiptPageRequests = [];
              window.fetch = async (input, options = {}) => {
                const url = new URL(
                  typeof input === "string" ? input : input.url,
                  window.location.origin,
                );
                if (url.pathname === "/api/receipts" && url.searchParams.has("page")) {
                  const requestedPage = url.searchParams.get("page");
                  window.__receiptPageRequests.push(requestedPage);
                  if (requestedPage === "2") {
                    return new Promise((resolve) => {
                      window.__releaseReceiptPageTwo = () => resolve(new Response(JSON.stringify({
                        items: [{
                          id: "page-two-receipt",
                          created_at: "2026-10-31T00:00:00Z",
                          status: "draft",
                          ocr_status: "succeeded",
                        }],
                        page: 2,
                        page_size: 20,
                        total: 21,
                        has_next: false,
                      }), { status: 200, headers: { "Content-Type": "application/json" } }));
                    });
                  }
                  return new Response(JSON.stringify({
                    items: [{
                      id: "page-one-receipt",
                      created_at: "2026-10-30T00:00:00Z",
                      status: "draft",
                      ocr_status: "succeeded",
                    }],
                    page: 1,
                    page_size: 20,
                    total: 21,
                    has_next: true,
                  }), { status: 200, headers: { "Content-Type": "application/json" } });
                }
                return nativeFetch(input, options);
              };
            })();"""
        )

        page.goto(app_url)
        expect(page.locator('[href="/receipts/page-one-receipt"]')).to_be_visible()
        next_page = page.get_by_role("button", name="次へ")
        next_page.click()
        page.wait_for_function("() => window.__receiptPageRequests.length === 2")
        expect(next_page).to_be_disabled()
        page.evaluate("document.querySelector('#next-page').dispatchEvent(new Event('click'))")
        assert page.evaluate("() => window.__receiptPageRequests") == ["1", "2"]

        page.evaluate("() => window.__releaseReceiptPageTwo()")
        expect(page.locator("#page-indicator")).to_have_text("2ページ")
        expect(page.locator('[href="/receipts/page-two-receipt"]')).to_be_visible()
        expect(next_page).to_be_disabled()
        page.get_by_role("button", name="前へ").click()
        expect(page.locator("#page-indicator")).to_have_text("1ページ")
        assert page.evaluate("() => window.__receiptPageRequests") == ["1", "2", "1"]
        browser.close()


def test_confirming_unchanged_draft_warns_before_unload_while_saving(
    app_url: str, fixture_dir: Path
) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.set_default_timeout(5_000)
        page.goto(app_url)
        page.set_input_files("#receipt-file", str(fixture_dir / "synthetic_receipt.png"))
        page.get_by_role("button", name="登録する").click()
        page.wait_for_url("**/receipts/*", timeout=5_000)
        page.get_by_role("button", name="＋ 明細を追加").click()
        page.get_by_label("商品名").fill("保存済みの明細")
        page.get_by_role("button", name="下書き保存").click()
        expect(page.locator("#save-indicator")).to_have_text("保存済み")

        page.reload()
        expect(page.locator(".item-card")).to_have_count(1)
        expect(page.locator("#save-indicator")).to_have_text("保存済み")
        assert not page.evaluate(
            """() => {
              const event = new Event("beforeunload", { cancelable: true });
              window.dispatchEvent(event);
              return event.defaultPrevented;
            }"""
        )

        page.evaluate(
            """() => {
              const nativeFetch = window.fetch.bind(window);
              window.fetch = (input, options = {}) => {
                const url = new URL(
                  typeof input === "string" ? input : input.url,
                  window.location.origin,
                );
                if (url.pathname.endsWith("/items") && options.method === "PUT") {
                  window.__confirmedStatus = JSON.parse(options.body).status;
                  return new Promise((resolve) => {
                    window.__releaseConfirmedSave = () => resolve(new Response(JSON.stringify({
                      revision: 2,
                      status: "confirmed",
                      created_at: "2026-01-01T00:00:00Z",
                      items: [{ id: "saved-item" }],
                    }), { status: 200, headers: { "Content-Type": "application/json" } }));
                  });
                }
                return nativeFetch(input, options);
              };
            }"""
        )

        page.get_by_role("button", name="確認して保存").click()
        expect(page.locator("#save-indicator")).to_have_text("保存中")
        page.wait_for_function("() => window.__confirmedStatus === 'confirmed'")
        assert page.evaluate(
            """() => {
              const event = new Event("beforeunload", { cancelable: true });
              window.dispatchEvent(event);
              return event.defaultPrevented;
            }"""
        )

        page.evaluate("() => window.__releaseConfirmedSave()")
        expect(page.locator("#save-indicator")).to_have_text("保存済み")
        browser.close()


def test_created_tag_remains_selectable_when_outside_initial_tag_limit(
    app_url: str, fixture_dir: Path
) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.set_default_timeout(5_000)
        page.goto(app_url)
        page.set_input_files("#receipt-file", str(fixture_dir / "synthetic_receipt.png"))
        page.get_by_role("button", name="登録する").click()
        page.wait_for_url("**/receipts/*", timeout=5_000)
        page.get_by_role("button", name="＋ 明細を追加").click()

        page.evaluate(
            """() => {
              const nativeFetch = window.fetch.bind(window);
              const existingTags = Array.from({ length: 100 }, (_, index) => ({
                id: `existing-${index}`,
                name: `既存タグ${String(index).padStart(3, "0")}`,
              }));
              window.fetch = (input, options = {}) => {
                const url = new URL(
                  typeof input === "string" ? input : input.url,
                  window.location.origin,
                );
                if (url.pathname === "/api/tags" && options.method === "POST") {
                  return Promise.resolve(new Response(JSON.stringify({
                    id: "created-tag",
                    name: "新規作成タグ",
                  }), { status: 201, headers: { "Content-Type": "application/json" } }));
                }
                if (url.pathname === "/api/tags") {
                  return Promise.resolve(new Response(JSON.stringify({ items: existingTags }), {
                    status: 200,
                    headers: { "Content-Type": "application/json" },
                  }));
                }
                return nativeFetch(input, options);
              };
            }"""
        )

        row = page.locator(".item-card").first
        row.locator(".tag-picker summary").click()
        row.get_by_label("タグを検索").fill("新規作成タグ")
        row.get_by_role("button", name="この名前でタグを作成").click()

        created_option = row.locator(".tag-option").filter(has_text="新規作成タグ")
        expect(created_option).to_have_count(1)
        expect(created_option.locator("input")).not_to_be_checked()
        page.wait_for_timeout(200)
        expect(created_option).to_have_count(1)
        browser.close()
