import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PERA = (ROOT / "live402/static/pera.js").read_text()
APP = (ROOT / "live402/static/app.js").read_text()
INDEX = (ROOT / "live402/static/index.html").read_text()


def _method_body(src, name):
    needle = "PeraWalletConnect.prototype.%s = function" % name
    i = src.find(needle)
    if i < 0:
        needle = "PeraWalletConnect.prototype.%s = async function" % name
        i = src.find(needle)
    if i < 0:
        raise AssertionError("missing method %s" % name)
    brace = src.find("{", i)
    depth = 0
    for j, ch in enumerate(src[brace:], brace):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[brace:j + 1]
    raise AssertionError("unclosed method %s" % name)


class PeraDeeplinkTests(unittest.TestCase):
    def test_begin_connect_is_sync_and_opens_before_await(self):
        self.assertIn("PeraWalletConnect.prototype.beginConnect = function ()", PERA)
        self.assertNotIn("PeraWalletConnect.prototype.beginConnect = async function", PERA)
        body = _method_body(PERA, "beginConnect")
        self.assertNotIn("await", body)
        self.assertIn("randomHex(32)", body)
        self.assertIn("this._clientId = uuid()", body)
        self.assertIn("this._handshakeTopic = uuid()", body)
        self.assertIn('this._peerId = ""', body)
        self.assertIn("this.connected = false", body)
        self.assertIn("return this._openPera(this._uri());", body)
        open_at = body.find("this._openPera")
        self.assertGreater(open_at, 0)

    def test_connect_once_opens_pera_before_socket(self):
        once = _method_body(PERA, "_connectOnce")
        open_at = once.find("this._openPera(this._uri())")
        await_at = once.find("await")
        self.assertGreater(open_at, 0)
        self.assertGreater(await_at, open_at)
        self.assertIn("if (!this._key || !this._handshakeTopic)", once)
        self.assertIn("await this._ensureSocket()", once)
        self.assertIn("wc_sessionRequest", once)
        after_open = once[open_at:]
        self.assertIn("await this._pub(this._handshakeTopic", after_open)

    def test_open_pera_scheme_and_last_deeplink(self):
        body = _method_body(PERA, "_openPera")
        self.assertIn('perawallet-wc://wc?uri=" + encodeURIComponent(uri)', body)
        self.assertIn("this.lastDeeplink = link", body)
        self.assertIn("window.location.href = link", body)
        self.assertIn("return link", body)

    def test_sign_transaction_refreshes_last_deeplink(self):
        body = _method_body(PERA, "signTransaction")
        pub_at = body.find("await this._pub")
        deeplink_at = body.find('this.lastDeeplink = "perawallet-wc://"')
        href_at = body.find('window.location.href = "perawallet-wc://"')
        self.assertGreater(pub_at, 0)
        self.assertGreater(deeplink_at, pub_at)
        self.assertGreater(href_at, pub_at)

    def test_reconnect_session_stays_async(self):
        self.assertIn("PeraWalletConnect.prototype.reconnectSession = async function", PERA)

    def test_click_calls_begin_connect_before_pay_algo(self):
        click = APP.split('payAlgoBtn.addEventListener("click"')[1]
        begin_at = click.find("beginConnect()")
        pay_at = click.find("payAlgo()")
        self.assertGreater(begin_at, 0)
        self.assertGreater(pay_at, begin_at)
        prefix = click[:begin_at]
        self.assertNotIn("await", prefix)

    def test_fallback_open_pera_link(self):
        self.assertIn('a.id = "open-pera"', APP)
        self.assertIn("Opening Pera", APP)
        self.assertIn('a.rel = "noopener"', APP)
        self.assertIn('a.target = "_self"', APP)
        self.assertIn("Open Pera", APP)
        self.assertNotIn('id="open-pera"', INDEX)

    def test_connect_reuses_pera_instance_and_skips_lute_on_iphone(self):
        self.assertIn("algoSession.pera || new window.PeraWalletConnect", APP)
        self.assertIn("window.LuteConnect && !isLikelyIphone()", APP)
        self.assertNotIn("window.lute || !isLikelyIphone()", APP)
        self.assertIn("if (!pera._launched)", APP)
        self.assertIn("await pera.reconnectSession()", APP)

    def test_sign_refreshes_open_pera_href(self):
        self.assertIn("ensureOpenPera(algoSession.pera.lastDeeplink)", APP)
        self.assertIn("signers: []", APP)
        self.assertIn("PAYMENT-SIGNATURE", APP)
        self.assertIn("paymentIndex: 1", APP)
        self.assertIn("paymentGroup: groupB64", APP)
        self.assertIn('"Algorand-Sender": from', APP)


if __name__ == "__main__":
    unittest.main()
