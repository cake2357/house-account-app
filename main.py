import uvicorn


def main() -> None:
    """ローカルWebアプリを単一ワーカーで起動する。"""
    uvicorn.run("app:create_app", factory=True, host="127.0.0.1", port=8000, workers=1)


if __name__ == "__main__":
    main()
