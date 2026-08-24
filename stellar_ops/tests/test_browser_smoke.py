import os
import unittest
from pathlib import Path

from playwright.sync_api import sync_playwright


@unittest.skipUnless(
    os.environ.get("RUN_BROWSER_E2E") == "1",
    "browser smoke tests run in the dedicated CI step",
)
class BrowserSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)
        cls.base_url = os.environ.get("STELLAR_OPS_BASE_URL", "http://127.0.0.1:5011")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        self.javascript_errors = []
        self.page.on("pageerror", lambda error: self.javascript_errors.append(str(error)))
        self.page.on(
            "console",
            lambda message: self.javascript_errors.append(message.text)
            if message.type == "error"
            and "Failed to load resource" not in message.text
            else None,
        )

    def tearDown(self):
        self.page.close()

    def assert_no_javascript_errors(self):
        self.assertEqual(self.javascript_errors, [])

    def test_operations_configuration_and_workspace_render(self):
        self.page.goto(f"{self.base_url}/ops", wait_until="networkidle")
        self.page.get_by_role("heading", name="Mission & Operation Control").wait_for()
        self.assert_no_javascript_errors()

        self.page.goto(f"{self.base_url}/control", wait_until="networkidle")
        self.page.get_by_role(
            "button", name="SYSTEM HEALTH & RECOVERY"
        ).click()
        self.page.locator("#system-health.active").wait_for()
        self.page.get_by_role(
            "button", name="RUN CONTROLLED SELF-TEST"
        ).wait_for()
        self.page.get_by_role(
            "button", name="CREATE VERIFIED BACKUP"
        ).wait_for()
        self.assertEqual(self.page.locator('[data-command]').count(), 0)
        self.assert_no_javascript_errors()

        self.page.goto(f"{self.base_url}/workspace", wait_until="domcontentloaded")
        self.page.get_by_text("MISSION CONTROL WORKSPACE").wait_for()
        self.page.get_by_role("button", name="PAUSE VIEW").click()
        self.page.get_by_role("button", name="REPLAY", exact=True).click()
        self.page.get_by_role("button", name="LIVE", exact=True).click()
        self.page.get_by_role("button", name="OPEN ALARM CENTER").click()
        self.page.locator("#alarm-dialog[open]").wait_for()
        self.page.get_by_role("button", name="Close alarm center").click()
        self.assert_no_javascript_errors()

    def test_critical_controls_have_accessible_names(self):
        self.page.goto(f"{self.base_url}/workspace", wait_until="domcontentloaded")
        for name in (
            "EDIT LAYOUT",
            "SAVE",
            "LOCKED",
            "PHASE AUTO",
            "+ PANEL",
            "INCIDENT CENTER",
            "KIOSK",
        ):
            self.page.get_by_role("button", name=name, exact=True).wait_for()
        self.page.get_by_role("combobox", name="Console profile").wait_for()
        self.page.get_by_role("combobox", name="Saved workspace").wait_for()
        self.assert_no_javascript_errors()




    def test_layout_dialog_popout_and_focus_restoration(self):
        self.page.goto(f"{self.base_url}/workspace", wait_until="domcontentloaded")
        self.page.get_by_role("button", name="EDIT LAYOUT", exact=True).click()
        initial_panels = self.page.locator("#workspace > .panel").count()

        self.page.get_by_role("button", name="+ PANEL", exact=True).click()
        self.page.locator("#panel-dialog[open]").wait_for()
        available = self.page.locator("#panel-catalog button:not([disabled])")
        if available.count():
            available.first.click()
            self.assertEqual(
                self.page.locator("#workspace > .panel").count(),
                initial_panels + 1,
            )
        else:
            self.page.get_by_role("button", name="Close panel catalog").click()

        popout_button = self.page.locator("[data-popout]").first
        with self.page.expect_popup() as popup_info:
            popout_button.click()
        popup = popup_info.value
        popup.wait_for_load_state("domcontentloaded")
        self.assertIn("/workspace?panel=", popup.url)
        popup.close()

        save = self.page.get_by_role("button", name="SAVE", exact=True)
        save.click()
        self.page.locator("#text-entry-dialog[open]").wait_for()
        self.page.get_by_role("button", name="CANCEL", exact=True).click()
        self.assertTrue(save.evaluate("element => element === document.activeElement"))
        self.assert_no_javascript_errors()

    def test_incident_lifecycle_uses_validated_dialogs(self):
        self.page.goto(f"{self.base_url}/workspace", wait_until="domcontentloaded")
        self.page.get_by_role("button", name="INCIDENT CENTER", exact=True).click()
        self.page.get_by_role("textbox", name="Incident title").fill(
            "Browser acceptance incident"
        )
        self.page.get_by_role("textbox", name="Incident description").fill(
            "Controlled incident created during automated browser acceptance."
        )
        self.page.get_by_role("button", name="OPEN INCIDENT", exact=True).click()
        self.page.locator("#incident-center").get_by_text("Browser acceptance incident").wait_for()

        self.page.get_by_role("button", name="RESOLVE", exact=True).last.click()
        self.page.locator("#text-entry-dialog[open]").wait_for()
        self.page.locator("#text-entry-value").fill(
            "Condition cleared and evidence reviewed."
        )
        self.page.get_by_role("button", name="CONTINUE", exact=True).click()
        self.page.get_by_text("RESOLVED", exact=True).last.wait_for()

        self.page.get_by_role("button", name="CLOSE", exact=True).last.click()
        self.page.locator("#text-entry-value").fill(
            "Closure approved during browser acceptance."
        )
        self.page.get_by_role("button", name="CONTINUE", exact=True).click()
        self.page.get_by_text("CLOSED", exact=True).last.wait_for()
        self.assert_no_javascript_errors()

    def test_stream_failure_enters_visible_reconnecting_state(self):
        self.page.route(
            "**/api/control/stream",
            lambda route: route.abort("connectionfailed"),
        )
        self.page.goto(f"{self.base_url}/workspace", wait_until="domcontentloaded")
        self.page.get_by_text("STREAM RECONNECTING").wait_for(timeout=10_000)
        self.assertEqual(
            self.page.locator("#footer-clock").inner_text(),
            "STREAM RECONNECTING",
        )

    def test_visible_controls_are_programmatically_named(self):
        audit_script = """() => {
            const visible = element => element.offsetParent !== null;
            const labelled = element => {
                const id = element.id;
                const explicit = id && document.querySelector(
                    'label[for="' + CSS.escape(id) + '"]'
                );
                const wrapped = element.closest('label');
                return Boolean(
                    explicit || wrapped || element.getAttribute('aria-label') ||
                    element.getAttribute('aria-labelledby') ||
                    element.getAttribute('title') ||
                    (element.tagName === 'BUTTON' && element.textContent.trim())
                );
            };
            return [...document.querySelectorAll('button,input,select,textarea')]
                .filter(visible)
                .filter(element => !labelled(element))
                .map(element => element.outerHTML.slice(0, 180));
        }"""
        for route in ("/ops", "/control", "/workspace"):
            with self.subTest(route=route):
                self.page.goto(f"{self.base_url}{route}", wait_until="domcontentloaded")
                unnamed = self.page.evaluate(audit_script)
                self.assertEqual(unnamed, [])
                self.assert_no_javascript_errors()


    def test_primary_layouts_have_no_page_level_overflow_and_capture_evidence(self):
        artifact_root = Path(
            os.environ.get("BROWSER_ARTIFACT_DIR", "/tmp/stellar-browser-artifacts")
        )
        artifact_root.mkdir(parents=True, exist_ok=True)
        for width, height, label in (
            (1440, 1000, "desktop"),
            (390, 844, "mobile"),
        ):
            self.page.set_viewport_size({"width": width, "height": height})
            for route, name in (
                ("/ops", "operations"),
                ("/control", "configuration"),
                ("/workspace", "mission-control"),
            ):
                with self.subTest(viewport=label, route=route):
                    self.page.goto(
                        f"{self.base_url}{route}",
                        wait_until="domcontentloaded",
                    )
                    dimensions = self.page.evaluate(
                        """() => ({
                            client: document.documentElement.clientWidth,
                            scroll: document.documentElement.scrollWidth
                        })"""
                    )
                    self.assertLessEqual(
                        dimensions["scroll"],
                        dimensions["client"] + 2,
                        f"page-level horizontal overflow at {route} / {label}",
                    )
                    self.page.screenshot(
                        path=str(artifact_root / f"{name}-{label}.png"),
                        full_page=True,
                    )
                    self.assert_no_javascript_errors()

    def test_visible_internal_navigation_has_no_broken_routes(self):
        self.page.goto(f"{self.base_url}/ops", wait_until="domcontentloaded")
        hrefs = self.page.locator('a[href^="/"]').evaluate_all(
            "(links) => [...new Set(links.map(link => link.getAttribute('href')))]"
        )
        failures = {}
        for href in hrefs:
            response = self.page.request.get(f"{self.base_url}{href}")
            if response.status >= 400:
                failures[href] = response.status
        self.assertEqual(failures, {})


if __name__ == "__main__":
    unittest.main()
