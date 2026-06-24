"""체험단 알림 서버 진입점.

한 프로세스에서 동시에 실행:
  1) 텔레그램 봇(명령어 수신) + 주기 폴링(신규 감지·알림)
  2) 웹 UI(FastAPI) — 텔레그램 로그인 + 키워드/지역 설정
같은 SQLite DB를 공유하므로 웹에서 바꾼 설정이 알림에 즉시 반영된다.
"""
from __future__ import annotations
import asyncio
import logging

import uvicorn
from telegram.ext import Application, CommandHandler

from app import config, db, bot
from app.poller import run_poll

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # 토큰 담긴 URL 로그 숨김
log = logging.getLogger("main")


async def _poll_job(ctx):
    await run_poll(ctx.bot, config.DEMO)


def _build_bot() -> Application:
    app = Application.builder().token(config.BOT_TOKEN).build()
    for name, fn in [
        ("start", bot.cmd_start), ("help", bot.cmd_help), ("add", bot.cmd_add),
        ("region", bot.cmd_region), ("del", bot.cmd_del), ("list", bot.cmd_list),
        ("clear", bot.cmd_clear), ("sites", bot.cmd_sites),
        ("stop", bot.cmd_stop), ("resume", bot.cmd_resume),
    ]:
        app.add_handler(CommandHandler(name, fn))
    app.job_queue.run_repeating(_poll_job, interval=config.POLL_INTERVAL, first=config.POLL_INTERVAL)
    return app


async def run() -> None:
    db.init(config.DB_PATH)
    application = _build_bot()
    mode = "DEMO" if config.DEMO else "실사이트"
    async def _initial_poll():
        try:
            await run_poll(application.bot, config.DEMO)  # 첫 수집(알림 생략). 백그라운드 실행.
        except Exception as e:
            log.warning("초기 수집 실패: %s", e)

    async with application:
        await application.start()
        await application.updater.start_polling()
        asyncio.create_task(_initial_poll())  # 웹은 즉시 기동, 수집은 뒤에서
        log.info("봇 시작 (%s 모드, 폴링 %d초)", mode, config.POLL_INTERVAL)
        if config.WEB_ENABLED:
            server = uvicorn.Server(uvicorn.Config(
                "app.web:app", host="0.0.0.0", port=config.WEB_PORT, log_level="warning"))
            log.info("웹 UI 시작: http://localhost:%d", config.WEB_PORT)
            await server.serve()
        else:
            while True:
                await asyncio.sleep(3600)
        await application.updater.stop()
        await application.stop()


def main() -> None:
    if not config.BOT_TOKEN:
        raise SystemExit("환경변수 TELEGRAM_BOT_TOKEN 이 없습니다. .env 에 넣어주세요.")
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit):
        log.info("종료")


if __name__ == "__main__":
    main()
