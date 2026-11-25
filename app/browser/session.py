from pathlib import Path
from playwright.async_api import async_playwright, BrowserContext, Page


class FirebaseBrowser:
    def __init__(self, profile_dir: str, headless: bool = False):
        self.profile_dir = Path(profile_dir).expanduser()
        self.headless = headless
        self._playwright = None
        self.context: BrowserContext | None = None

    async def start(self) -> Page:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = await async_playwright().start()
        try:
            self.context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir.resolve()),
                channel="chromium",
                headless=self.headless,
                viewport={"width": 1440, "height": 1000},
                args=["--disable-blink-features=AutomationControlled"],
            )
            page = self.context.pages[0] if self.context.pages else await self.context.new_page()
            page.set_default_navigation_timeout(45_000)
            page.set_default_timeout(15_000)
            return page
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        if self.context:
            await self.context.close()
            self.context = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
