import asyncio
import os
import random
from asyncio import Task
from typing import Dict, List, Optional, Tuple

from playwright.async_api import (BrowserContext, BrowserType, Page,
                                  async_playwright)

import config
from base.base_crawler import AbstractCrawler
from proxy.proxy_ip_pool import IpInfoModel, create_ip_pool
from store import xhs as xhs_store
from tools import utils
from var import crawler_type_var

from .client import XiaoHongShuClient
from .exception import DataFetchError
from .field import SearchSortType
from .login import XiaoHongShuLogin


class XiaoHongShuCrawler(AbstractCrawler):
    context_page: Page
    xhs_client: XiaoHongShuClient
    browser_context: BrowserContext

    def __init__(self) -> None:
        self.index_url = "https://www.xiaohongshu.com"
        self.user_agent = utils.get_user_agent()

    async def start(self) -> None:
        # while True:  # 添加一个无限循环 让程序一直运行 (====>>不启用<<====)
            playwright_proxy_format, httpx_proxy_format = None, None
            if config.ENABLE_IP_PROXY:
                ip_proxy_pool = await create_ip_pool(config.IP_PROXY_POOL_COUNT, enable_validate_ip=True)
                ip_proxy_info: IpInfoModel = await ip_proxy_pool.get_proxy()
                playwright_proxy_format, httpx_proxy_format = self.format_proxy_info(ip_proxy_info)

            async with async_playwright() as playwright:
                # Launch a browser context.
                # 启动浏览器上下文。
                chromium = playwright.chromium
                self.browser_context = await self.launch_browser(
                    chromium,
                    None,
                    self.user_agent,
                    headless=config.HEADLESS
                )
                # stealth.min.js is a js script to prevent the website from detecting the crawler.
                # Stealth.min.js是一个js脚本，用来防止网站检测到爬虫。
                await self.browser_context.add_init_script(path="libs/stealth.min.js")
                # add a cookie attribute webId to avoid the appearance of a sliding captcha on the webpage
                # 添加一个cookie属性webId，以避免在网页上出现滑动验证码
                await self.browser_context.add_cookies([{
                    'name': "webId",
                    'value': "xxx123",  # any value
                    'domain': ".xiaohongshu.com",
                    'path': "/"
                }])
                self.context_page = await self.browser_context.new_page()
                await self.context_page.goto(self.index_url)

                # Create a client to interact with the xiaohongshu website.
                # 创建一个客户端与小红书网站进行交互。
                self.xhs_client = await self.create_xhs_client(httpx_proxy_format)
                if not await self.xhs_client.pong():
                    login_obj = XiaoHongShuLogin(
                        login_type=config.LOGIN_TYPE,
                        login_phone="",  # input your phone number
                        browser_context=self.browser_context,
                        context_page=self.context_page,
                        cookie_str=config.COOKIES
                    )
                    await login_obj.begin()
                    await self.xhs_client.update_cookies(browser_context=self.browser_context)

                crawler_type_var.set(config.CRAWLER_TYPE)
                if config.CRAWLER_TYPE == "search":
                    # Search for notes and retrieve their comment information.
                    # 搜索笔记并检索它们的评论信息。
                    await self.search()
                elif config.CRAWLER_TYPE == "detail":
                    # Get the information and comments of the specified post
                    # 获取指定帖子的信息和评论
                    await self.get_specified_notes()
                elif config.CRAWLER_TYPE == "creator":
                    # Get creator's information and their notes and comments
                    # 获取创作者的信息和他们的笔记和评论
                    await self.get_creators_and_notes()
                else:
                    pass

                utils.logger.info("[XiaoHongShuCrawler.start] 小红书爬取完成")

                # await asyncio.sleep(60)  # 等待一段时间后重新开始 (====>>不启用<<====)

    async def search(self) -> None:
        """Search for notes and retrieve their comment information."""
        # 搜索笔记并检索它们的评论信息。
        utils.logger.info("[XiaoHongShuCrawler.search] Begin search xiaohongshu keywords")
        xhs_limit_count = 20  # xhs limit page fixed value
        if config.CRAWLER_MAX_NOTES_COUNT < xhs_limit_count:
            config.CRAWLER_MAX_NOTES_COUNT = xhs_limit_count
        start_page = config.START_PAGE
        for keyword in config.KEYWORDS.split(","):
            utils.logger.info(f"[XiaoHongShuCrawler.search] Current search keyword: {keyword}")
            page = 1
            while (page - start_page + 1) * xhs_limit_count <= config.CRAWLER_MAX_NOTES_COUNT:
                if page < start_page:
                    utils.logger.info(f"[XiaoHongShuCrawler.search] Skip page {page}")
                    page += 1
                    continue

                try:
                    utils.logger.info(f"[XiaoHongShuCrawler.search] search xhs keyword: {keyword}, page: {page}")
                    note_id_list: List[str] = []
                    notes_res = await self.xhs_client.get_note_by_keyword(
                        keyword=keyword,
                        page=page,
                        sort=SearchSortType(config.SORT_TYPE) if config.SORT_TYPE != '' else SearchSortType.GENERAL,
                    )
                    utils.logger.info(f"[XiaoHongShuCrawler.search] Search notes res:{notes_res}")
                    semaphore = asyncio.Semaphore(config.MAX_CONCURRENCY_NUM)
                    task_list = [
                        self.get_note_detail(post_item.get("id"), semaphore)
                        for post_item in notes_res.get("items", {})
                        if post_item.get('model_type') not in ('rec_query', 'hot_query')
                    ]
                    note_details = await asyncio.gather(*task_list)
                    for note_detail in note_details:
                        if note_detail is not None:
                            await xhs_store.update_xhs_note(note_detail)
                            note_id_list.append(note_detail.get("note_id"))
                    page += 1
                    utils.logger.info(f"[XiaoHongShuCrawler.search] Note details: {note_details}")
                    await self.batch_get_note_comments(note_id_list)
                except DataFetchError:
                    utils.logger.error("[XiaoHongShuCrawler.search] Get note detail error")
                    break

    async def get_creators_and_notes(self) -> None:
        """Get creator's notes and retrieve their comment information."""
        # 获取创建者的注释并检索他们的评论信息。
        utils.logger.info("[XiaoHongShuCrawler.get_creators_and_notes] Begin get xiaohongshu creators")
        for user_id in config.XHS_CREATOR_ID_LIST:
            # get creator detail info from web html content
            # 从web HTML内容获取创建者详细信息
            createor_info: Dict = await self.xhs_client.get_creator_info(user_id=user_id)
            if createor_info:
                await xhs_store.save_creator(user_id, creator=createor_info)

            # Get all note information of the creator
            # 获取创建者的所有注释信息
            all_notes_list = await self.xhs_client.get_all_notes_by_creator(
                user_id=user_id,
                crawl_interval=random.random(),
                callback=self.fetch_creator_notes_detail
            )

            note_ids = [note_item.get("note_id") for note_item in all_notes_list]
            await self.batch_get_note_comments(note_ids)

    async def fetch_creator_notes_detail(self, note_list: List[Dict]):
        """
        Concurrently obtain the specified post list and save the data
        """
        # 并发获取指定的岗位列表并保存数据
        semaphore = asyncio.Semaphore(config.MAX_CONCURRENCY_NUM)
        task_list = [
            self.get_note_detail(post_item.get("note_id"), semaphore) for post_item in note_list
        ]

        note_details = await asyncio.gather(*task_list)
        for note_detail in note_details:
            if note_detail is not None:
                await xhs_store.update_xhs_note(note_detail)

    async def get_specified_notes(self):
        """Get the information and comments of the specified post"""
        # 获取指定帖子的信息和评论
        semaphore = asyncio.Semaphore(config.MAX_CONCURRENCY_NUM)
        task_list = [
            self.get_note_detail(note_id=note_id, semaphore=semaphore) for note_id in config.XHS_SPECIFIED_ID_LIST
        ]
        note_details = await asyncio.gather(*task_list)
        for note_detail in note_details:
            if note_detail is not None:
                await xhs_store.update_xhs_note(note_detail)
        await self.batch_get_note_comments(config.XHS_SPECIFIED_ID_LIST)

    async def get_note_detail(self, note_id: str, semaphore: asyncio.Semaphore) -> Optional[Dict]:
        """Get note detail"""
        # 获取笔记细节
        async with semaphore:
            try:
                return await self.xhs_client.get_note_by_id(note_id)
            except DataFetchError as ex:
                utils.logger.error(f"[XiaoHongShuCrawler.get_note_detail] Get note detail error: {ex}")
                return None
            except KeyError as ex:
                utils.logger.error(
                    f"[XiaoHongShuCrawler.get_note_detail] have not fund note detail note_id:{note_id}, err: {ex}")
                return None

    async def batch_get_note_comments(self, note_list: List[str]):
        """Batch get note comments"""
        # 批量获取注释
        if not config.ENABLE_GET_COMMENTS:
            utils.logger.info(f"[XiaoHongShuCrawler.batch_get_note_comments] Crawling comment mode is not enabled")
            return

        utils.logger.info(
            f"[XiaoHongShuCrawler.batch_get_note_comments] Begin batch get note comments, note list: {note_list}")
        semaphore = asyncio.Semaphore(config.MAX_CONCURRENCY_NUM)
        task_list: List[Task] = []
        for note_id in note_list:
            task = asyncio.create_task(self.get_comments(note_id, semaphore), name=note_id)
            task_list.append(task)
        await asyncio.gather(*task_list)

    async def get_comments(self, note_id: str, semaphore: asyncio.Semaphore):
        """Get note comments with keyword filtering and quantity limitation"""
        # 通过关键字过滤和数量限制获取注释
        async with semaphore:
            utils.logger.info(f"[XiaoHongShuCrawler.get_comments] Begin get note id comments {note_id}")
            await self.xhs_client.get_note_all_comments(
                note_id=note_id,
                crawl_interval=random.random(),
                callback=xhs_store.batch_update_xhs_note_comments
            )

    @staticmethod
    def format_proxy_info(ip_proxy_info: IpInfoModel) -> Tuple[Optional[Dict], Optional[Dict]]:
        """format proxy info for playwright and httpx"""
        # 为剧作家和HTTPX格式化代理信息
        playwright_proxy = {
            "server": f"{ip_proxy_info.protocol}{ip_proxy_info.ip}:{ip_proxy_info.port}",
            "username": ip_proxy_info.user,
            "password": ip_proxy_info.password,
        }
        httpx_proxy = {
            f"{ip_proxy_info.protocol}": f"http://{ip_proxy_info.user}:{ip_proxy_info.password}@{ip_proxy_info.ip}:{ip_proxy_info.port}"
        }
        return playwright_proxy, httpx_proxy

    async def create_xhs_client(self, httpx_proxy: Optional[str]) -> XiaoHongShuClient:
        """Create xhs client"""
        # 创建xhs客户端
        utils.logger.info("[XiaoHongShuCrawler.create_xhs_client] Begin create xiaohongshu API client ...")
        cookie_str, cookie_dict = utils.convert_cookies(await self.browser_context.cookies())
        xhs_client_obj = XiaoHongShuClient(
            proxies=httpx_proxy,
            headers={
                "User-Agent": self.user_agent,
                "Cookie": cookie_str,
                "Origin": "https://www.xiaohongshu.com",
                "Referer": "https://www.xiaohongshu.com",
                "Content-Type": "application/json;charset=UTF-8"
            },
            playwright_page=self.context_page,
            cookie_dict=cookie_dict,
        )
        return xhs_client_obj

    async def launch_browser(
            self,
            chromium: BrowserType,
            playwright_proxy: Optional[Dict],
            user_agent: Optional[str],
            headless: bool = True
    ) -> BrowserContext:
        """Launch browser and create browser context"""
        # 启动浏览器并创建浏览器上下文
        utils.logger.info("[XiaoHongShuCrawler.launch_browser] Begin create browser context ...")
        if config.SAVE_LOGIN_STATE:
            # feat issue #14
            # 第14期
            # we will save login state to avoid login every time
            # 我们会保存登录状态，避免每次登录
            user_data_dir = os.path.join(os.getcwd(), "browser_data",
                                         config.USER_DATA_DIR % config.PLATFORM)  # type: ignore
            browser_context = await chromium.launch_persistent_context(
                user_data_dir=user_data_dir,
                accept_downloads=True,
                headless=headless,
                proxy=playwright_proxy,  # type: ignore
                viewport={"width": 1920, "height": 1080},
                user_agent=user_agent
            )
            return browser_context
        else:
            browser = await chromium.launch(headless=headless, proxy=playwright_proxy)  # type: ignore
            browser_context = await browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent=user_agent
            )
            return browser_context

    async def close(self):
        """Close browser context"""
        # 关闭浏览器上下文
        await self.browser_context.close()
        utils.logger.info("[XiaoHongShuCrawler.close] Browser context closed ...")