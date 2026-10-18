"""One folder, one process, one website client, two independent Telegram bots."""

import asyncio
import logging
import os
import signal

from .bgmu import BGMUSource
from .profiles import ProfileStore
from .refresh import stop_background
from .shared_schedule import SharedSchedule
from .ui import create_application
from .website import WebsiteClient


async def public_polling(application):
    async with application:
        await application.start()
        try:
            await application.updater.start_polling(bootstrap_retries=0, drop_pending_updates=False)
            await asyncio.Event().wait()
        finally:
            if application.updater.running:
                await application.updater.stop()
            if application.running:
                await application.stop()


async def refresh_feed(feed):
    while True:
        await feed.refresh_safely()
        await asyncio.sleep(60)


async def run_together(token):
    if os.environ['RESTRICTED_BOT_TOKEN'].strip() == token:
        raise ValueError('Two distinct Telegram bot identities are required')
    from restricted_bot import bot as restricted
    from .__main__ import RedactingFormatter
    for handler in logging.getLogger().handlers:
        handler.setFormatter(RedactingFormatter([token, restricted.BOT_TOKEN, os.environ.get('BGMU_PROXY_URL', '')]))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed = []
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop.set)
            installed.append(signum)
        except NotImplementedError:
            pass
    tasks = []
    try:
        async with WebsiteClient.from_environment() as website:
            source = BGMUSource(website, cache_ttl=300)
            feed = SharedSchedule(source)
            source.schedule_consumers = [feed]
            profiles = ProfileStore.from_environment()
            application = create_application(token, source, profiles=profiles)
            await feed.refresh_safely()
            tasks = [asyncio.create_task(public_polling(application)),
                     asyncio.create_task(restricted.run_restricted(feed, handle_signals=False)),
                     asyncio.create_task(refresh_feed(feed)), asyncio.create_task(stop.wait())]
            try:
                finished, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in finished:
                    task.result()
            finally:
                await stop_background(source)
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        await restricted.bot.session.close()
        for signum in installed:
            loop.remove_signal_handler(signum)
