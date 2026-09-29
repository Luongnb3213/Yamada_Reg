import shutil
import time
import tempfile
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright, BrowserContext, Page
import src.config as config
from src.utils.logger import get_logger

log = get_logger("browser")

class BrowserInstance:
    def __init__(self, worker_id: int, proxy: dict | None = None):
        self.worker_id = worker_id
        self.proxy = proxy
        self.playwright = None
        self.context = None
        self.profile_dir = Path(tempfile.gettempdir()) / f"yamada_browser_worker_{worker_id}"

    async def start(self) -> Page:
        """Khởi động Cloak Browser với proxy và profile tạm thời."""
        self.cleanup_profile_dir()
        self.profile_dir.mkdir(parents=True, exist_ok=True)

        self._playwright_cm = async_playwright()
        self.playwright = await self._playwright_cm.__aenter__()

        # Track active browser instances so callers can close them on STOP.
        if not hasattr(config, "ACTIVE_BROWSERS"):
            config.ACTIVE_BROWSERS = []
        config.ACTIVE_BROWSERS.append(self)

        # Cấu hình proxy cho Playwright — phải truyền đủ server + username + password
        launch_args = {}
        if self.proxy:
            playwright_proxy = {
                "server": self.proxy["server"],
            }
            if "username" in self.proxy:
                playwright_proxy["username"] = self.proxy["username"]
            if "password" in self.proxy:
                playwright_proxy["password"] = self.proxy["password"]
            launch_args["proxy"] = playwright_proxy
            log.info(f"Khởi động trình duyệt với proxy: {self.proxy['server']} | user={self.proxy.get('username', 'none')}")
        else:
            log.info("Khởi động trình duyệt không dùng proxy")

        executable_path = config.BROWSER_PATH if config.BROWSER_PATH else None
        
        # Sửa lỗi EACCES trên macOS khi truyền thư mục .app thay vì file thực thi
        if executable_path and executable_path.endswith(".app"):
            import platform
            import os
            if platform.system() == "Darwin":
                app_name = os.path.basename(executable_path).replace(".app", "")
                executable_path = os.path.join(executable_path, "Contents", "MacOS", app_name)
        log.info(f"Profile dir: {self.profile_dir}")

        base_args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--inprivate"
        ]

        if config.HEADLESS:
            base_args.extend(["--headless=new", "--window-position=-32000,-32000"])

        self.browser = None
        if executable_path:
            log.info(f"Dùng trình duyệt tùy chỉnh: {executable_path}")
            self.browser = await self.playwright.chromium.launch(
                executable_path=executable_path,
                headless=config.HEADLESS,
                args=base_args,
                **launch_args
            )
        else:
            # Thử Chrome trước trên mọi hệ điều hành, Edge làm dự phòng, rồi tới Chromium mặc định của Playwright
            channels_to_try = ["chrome", "msedge", None]
            
            for channel in channels_to_try:
                try:
                    ch_label = channel if channel else "chromium (Playwright)"
                    log.info(f"Đang thử khởi động với trình duyệt: {ch_label}")
                    
                    launch_kwargs = {
                        "headless": config.HEADLESS,
                        "args": base_args,
                        **launch_args
                    }
                    if channel:
                        launch_kwargs["channel"] = channel

                    self.browser = await self.playwright.chromium.launch(**launch_kwargs)
                    log.info(f"✅ Khởi động thành công với: {ch_label}")
                    break
                except Exception as e:
                    ch_label = channel if channel else "chromium (Playwright)"
                    log.warning(f"⚠️ Không thể khởi động bằng {ch_label}: {e}")
            
            if not self.browser:
                raise Exception("Không thể khởi động trình duyệt (Chrome, Edge hoặc Chromium)!")

        # Lớp bảo vệ khi cùng một BrowserInstance bị gọi start() nhiều lần:
        # đóng context cũ trước khi tạo context mới.
        if self.context:
            await self.context.close()
            self.context = None

        # Default to Japanese locale/timezone because Yamada flows usually target JP pages.
        self.context = await self.browser.new_context(
            ignore_https_errors=True,
            viewport={"width": 1280, "height": 800},
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
            extra_http_headers={"Accept-Language": "ja-JP,ja;q=0.9,en;q=0.8"},
        )
        page = await self.context.new_page()
        # Đặt timeout mặc định 90s cho môi trường proxy chậm
        page.set_default_timeout(90000)
        page.set_default_navigation_timeout(90000)

        # CHẾ ĐỘ TIẾT KIỆM BĂNG THÔNG: Chặn tải ảnh, video, font chữ
        async def block_heavy_resources(route):
            try:
                if route.request.resource_type in ["image", "media", "font"]:
                    await route.abort()
                else:
                    await route.continue_()
            except Exception:
                pass
        
        await self.context.route("**/*", block_heavy_resources)
        log.info("✅ Đã bật chế độ tiết kiệm băng thông (chặn ảnh/video/font) cho toàn bộ trình duyệt")

        # THEO DÕI DATA SỬ DỤNG
        self.total_bytes = 0
        async def on_request_finished(request):
            try:
                sizes = await request.sizes()
                if sizes:
                    self.total_bytes += sizes.get("requestBodySize", 0)
                    self.total_bytes += sizes.get("requestHeadersSize", 0)
                    self.total_bytes += sizes.get("responseBodySize", 0)
                    self.total_bytes += sizes.get("responseHeadersSize", 0)
            except Exception:
                pass
                
        self.context.on("requestfinished", on_request_finished)

        # Thiết lập Virtual WebAuthn Authenticator để chặn popup Bluetooth/USB Passkey của Chrome
        try:
            cdp = await self.context.new_cdp_session(page)
            await cdp.send("WebAuthn.enable")
            await cdp.send("WebAuthn.addVirtualAuthenticator", {
                "options": {
                    "protocol": "ctap2",
                    "transport": "usb",
                    "hasResidentKey": True,
                    "hasUserVerification": True,
                    "isUserVerified": True,
                    "automaticPresenceSimulation": True,
                }
            })
            log.info("✅ Đã kích hoạt Virtual WebAuthn Authenticator.")
        except Exception as e:
            log.warning(f"⚠️ Không thể kích hoạt Virtual WebAuthn: {e}")

        await page.wait_for_timeout(500)
        return page


    def get_data_usage_mb(self) -> float:
        """Trả về dung lượng data đã sử dụng tính bằng MB."""
        return getattr(self, 'total_bytes', 0) / (1024 * 1024)

    async def close(self):
        """Đóng trình duyệt và xóa sạch toàn bộ data (profile dir)."""
        log.info(f"Đang đóng trình duyệt Worker {self.worker_id}...")

        if self.context:
            # Mỗi account dùng một BrowserContext riêng. Chủ động xóa toàn bộ
            # trạng thái web trước khi đóng để cookie/session lỗi của account
            # trước không thể ảnh hưởng account sau, kể cả khi website dùng
            # localStorage, sessionStorage hoặc Cache Storage.
            try:
                for page in list(self.context.pages):
                    try:
                        await page.evaluate("""async () => {
                            try { localStorage.clear(); } catch (_) {}
                            try { sessionStorage.clear(); } catch (_) {}
                            try {
                                if ('caches' in window) {
                                    const names = await caches.keys();
                                    await Promise.all(names.map(name => caches.delete(name)));
                                }
                            } catch (_) {}
                        }""")
                    except Exception:
                        pass
                await self.context.clear_cookies()
                await self.context.clear_permissions()
                log.info("🧼 Đã xóa cookies, storage, cache và permissions của account hiện tại.")
            except Exception as e:
                log.debug(f"Không thể xóa toàn bộ browser state trước khi đóng: {e}")

            try:
                await asyncio.wait_for(self.context.close(), timeout=10.0)
            except Exception as e:
                log.warning(f"Lỗi khi đóng context: {e}")
            self.context = None

        if hasattr(self, 'browser') and self.browser:
            try:
                await asyncio.wait_for(self.browser.close(), timeout=5.0)
            except Exception as e:
                log.debug(f"Lỗi khi đóng browser ({type(e).__name__}): {e}")
            self.browser = None

        if self.playwright:
            try:
                await asyncio.wait_for(self._playwright_cm.__aexit__(None, None, None), timeout=5.0)
            except Exception as e:
                log.debug(f"Lỗi khi stop playwright ({type(e).__name__}): {e}")
            self.playwright = None
            self._playwright_cm = None

        # Xóa khỏi ACTIVE_BROWSERS
        if hasattr(config, "ACTIVE_BROWSERS") and self in config.ACTIVE_BROWSERS:
            try:
                config.ACTIVE_BROWSERS.remove(self)
            except ValueError:
                pass

        self.cleanup_profile_dir()

    def cleanup_profile_dir(self):
        """Xóa sạch thư mục profile tạm thời của browser."""
        if self.profile_dir.exists():
            log.info(f"🧹 Xóa data browser tại: {self.profile_dir}")
            for attempt in range(5):
                try:
                    shutil.rmtree(self.profile_dir, ignore_errors=True)
                    if not self.profile_dir.exists():
                        log.info("✅ Xóa data browser thành công.")
                        break
                except Exception as e:
                    log.warning(f"Thử xóa lần {attempt+1} lỗi: {e}")
                time.sleep(1)

            if self.profile_dir.exists():
                try:
                    shutil.rmtree(self.profile_dir)
                    log.info("✅ Cưỡng chế xóa data browser thành công.")
                except Exception as e:
                    log.error(f"❌ Không thể xóa thư mục profile: {e}")
