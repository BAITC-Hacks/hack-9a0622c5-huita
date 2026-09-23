"""Exercise the shipped API module with the optional system JavaScript engine.

There is no Node or package installation requirement. API behavior runs on macOS
JavaScriptCore (or its Linux system library); HTML accessibility checks run on all
platforms. Browser layout and native file/dialog behavior need browser QA.
"""

import ctypes
import ctypes.util
import json
from html.parser import HTMLParser
from pathlib import Path

import pytest


STATIC = Path(__file__).resolve().parents[1] / "static"
APP_FUNCTIONS = (STATIC / "app.js").read_text().split("\n", 1)[1].split("$('server-address').textContent", 1)[0]
DOM_DOUBLES = """
    class Element {
        constructor() {
            this.value = ''; this.textContent = ''; this.hidden = false;
            this.attributes = new Map(); this.children = []; this.listeners = {};
            this.descendants = new Map(); this.dataset = {};
            this.classList = { toggle() {}, add() {}, remove() {} };
        }
        setAttribute(key, value) { this.attributes.set(key, String(value)); }
        getAttribute(key) { return this.attributes.get(key) ?? null; }
        removeAttribute(key) { this.attributes.delete(key); }
        querySelector(selector) {
            if (!this.descendants.has(selector)) this.descendants.set(selector, new Element());
            return this.descendants.get(selector);
        }
        append(...children) { this.children.push(...children); }
        replaceChildren(...children) { this.children = children; }
        addEventListener(name, listener) { this.listeners[name] = listener; }
        focus() { document.activeElement = this; }
    }
    const elements = new Map();
    const element = key => { if (!elements.has(key)) elements.set(key, new Element()); return elements.get(key); };
    globalThis.document = {
        getElementById: id => element('#' + id), querySelector: element,
        querySelectorAll: () => [], createElement: () => new Element(),
        createElementNS: () => new Element(), createDocumentFragment: () => new Element(),
        hidden: false,
    };
    globalThis.fetch = async () => response({});
"""


class Elements(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.items = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        self.items.append((tag, dict(attrs)))


def test_collapsed_navigation_and_scroll_regions_remain_accessible():
    elements = Elements((STATIC / "index.html").read_text()).items
    ids = {attrs["id"] for _, attrs in elements if "id" in attrs}
    nav = [attrs for tag, attrs in elements if tag == "a" and "nav-item" in attrs.get("class", "").split()]
    assert nav
    for attrs in nav:
        # The visible span is hidden in the compact navigation layout.
        assert attrs.get("aria-label", "").strip()
        assert attrs["href"].removeprefix("#") in ids
    scroll = [attrs for _, attrs in elements if "table-scroll" in attrs.get("class", "").split() or attrs.get("id") == "event-list"]
    assert len(scroll) == 2
    for attrs in scroll:
        assert attrs.get("tabindex") == "0"
        assert attrs.get("aria-label", "").strip()


@pytest.fixture
def javascript():
    library = next((name for name in (
        ctypes.util.find_library("JavaScriptCore"),
        ctypes.util.find_library("javascriptcoregtk-4.1"),
        ctypes.util.find_library("javascriptcoregtk-4.0"),
    ) if name), None)
    if library is None:
        pytest.skip("Optional system JavaScriptCore is unavailable; browser QA covers these scenarios")
    js = ctypes.CDLL(library)
    pointer = ctypes.c_void_p
    signatures = {
        "JSGlobalContextCreate": ([pointer], pointer),
        "JSGlobalContextRelease": ([pointer], None),
        "JSStringCreateWithUTF8CString": ([ctypes.c_char_p], pointer),
        "JSStringRelease": ([pointer], None),
        "JSEvaluateScript": ([pointer, pointer, pointer, pointer, ctypes.c_int, ctypes.POINTER(pointer)], pointer),
        "JSValueToStringCopy": ([pointer, pointer, ctypes.POINTER(pointer)], pointer),
        "JSStringGetMaximumUTF8CStringSize": ([pointer], ctypes.c_size_t),
        "JSStringGetUTF8CString": ([pointer, ctypes.c_char_p, ctypes.c_size_t], ctypes.c_size_t),
    }
    for name, (args, result) in signatures.items():
        getattr(js, name).argtypes = args
        getattr(js, name).restype = result
    context = js.JSGlobalContextCreate(None)

    def evaluate(source):
        script = js.JSStringCreateWithUTF8CString(source.encode())
        error = pointer()
        try:
            value = js.JSEvaluateScript(context, script, None, None, 1, ctypes.byref(error))
            text = js.JSValueToStringCopy(context, error.value or value, None)
            try:
                size = js.JSStringGetMaximumUTF8CStringSize(text)
                buffer = ctypes.create_string_buffer(size)
                js.JSStringGetUTF8CString(text, buffer, size)
                result = buffer.value.decode()
            finally:
                js.JSStringRelease(text)
        finally:
            js.JSStringRelease(script)
        if error.value:
            raise AssertionError(result)
        return result

    # Small Web API doubles keep requests entirely offline. The code under test
    # is the real module, including its private request/cancellation methods.
    evaluate("""
        globalThis.timers = new Map();
        globalThis.setTimeout = fn => { const id = {}; timers.set(id, fn); return id; };
        globalThis.clearTimeout = id => timers.delete(id);
        globalThis.Headers = class {
            constructor(init = {}) { this.values = new Map(); for (const [k, v] of Object.entries(init)) this.set(k, v); }
            set(k, v) { this.values.set(k.toLowerCase(), String(v)); }
            get(k) { return this.values.get(k.toLowerCase()) ?? null; }
        };
        globalThis.AbortController = class {
            constructor() { this.signal = { aborted: false }; }
            abort() { this.signal.aborted = true; }
        };
        globalThis.deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };
        globalThis.response = (data, status = 200, headers = {}) => ({
            ok: status >= 200 && status < 300, status, headers: new Headers(headers),
            text: async () => JSON.stringify(data), blob: async () => 'downloaded-blob',
        });
    """)
    evaluate((STATIC / "api.js").read_text().replace("export class ", "class "))
    app = (STATIC / "app.js").read_text().split("\n", 1)[1]
    evaluate(f"new Function({json.dumps(app)}); 'compiled'")

    def run(body):
        evaluate("globalThis.result = undefined; globalThis.failure = undefined;")
        evaluate(f"void (async () => {{ {body} }})().then(value => {{ result = value; }}, error => {{ failure = String(error); }});")
        # JavaScriptCore drains Promise jobs after each script evaluation.
        failure = evaluate("failure === undefined ? '' : failure")
        assert not failure, failure
        value = evaluate("JSON.stringify(result)")
        assert value != "undefined", "Scenario left an unsettled request"
        return json.loads(value)

    try:
        yield run
    finally:
        js.JSGlobalContextRelease(context)


def test_refresh_cancels_reads_but_preserves_download_and_post(javascript):
    result = javascript("""
        const requests = [];
        const api = new BeeSmartApi({ fetchImpl: (url, options) => {
            const gate = deferred(); requests.push({ url, options, gate }); return gate.promise;
        }});
        const read = api.overview().catch(error => ({ aborted: error.aborted }));
        const download = api.downloadRun('run-1', 'csv');
        const post = api.startRun(42);
        api.cancelReadRequests();
        const aborted = requests.map(item => item.options.signal.aborted);
        requests.forEach(item => item.gate.resolve(response({ id: 'run-1' })));
        return { aborted, read: await read, download: (await download).blob, post: await post,
            marker: requests[2].options.headers.get('X-BeeSmart-Request'), timers: timers.size };
    """)
    assert result == {"aborted": [True, False, False], "read": {"aborted": True}, "download": "downloaded-blob", "post": {"id": "run-1"}, "marker": "1", "timers": 0}


def test_token_switch_discards_already_started_download_body(javascript):
    result = javascript("""
        const body = deferred();
        const requests = [];
        const api = new BeeSmartApi({ token: 'old-test-token', fetchImpl: async (url, options) => {
            requests.push(options); return { ...response({}), blob: () => body.promise };
        }});
        const pending = api.downloadRun('run-1', 'json').catch(error => ({ aborted: error.aborted }));
        await Promise.resolve();
        api.setToken('new-test-token');
        body.resolve('private-report');
        const outcome = await pending;
        await api.overview();
        return { outcome, oldAborted: requests[0].signal.aborted,
            newAuth: requests[1].headers.get('Authorization'), timers: timers.size };
    """)
    assert result == {"outcome": {"aborted": True}, "oldAborted": True, "newAuth": "Bearer new-test-token", "timers": 0}


def test_request_errors_redact_token_and_keep_server_retry_delay(javascript):
    result = javascript("""
        const api = new BeeSmartApi({ token: 'qa-secret', fetchImpl: async () => response(
            { detail: [{ msg: 'Rejected qa-secret and Bearer hidden-secret', input: 'raw-private-input' }] },
            429, { 'Retry-After': '12' }
        )});
        try { await api.overview(); }
        catch (error) { return { status: error.status, message: error.message, retryAfter: error.retryAfter }; }
    """)
    assert result["status"] == 429
    assert result["retryAfter"] == 12
    assert "[скрыто]" in result["message"]
    assert all(secret not in result["message"] for secret in ("qa-secret", "hidden-secret", "raw-private-input"))


def test_timeout_is_not_mistaken_for_deliberate_cancellation(javascript):
    result = javascript("""
        const gate = deferred();
        const api = new BeeSmartApi({ fetchImpl: () => gate.promise });
        const pending = api.startRun(42).catch(error => ({ status: error.status, aborted: !!error.aborted }));
        Array.from(timers.values()).forEach(fn => fn());
        gate.resolve(response({ id: 'uncertain-run' }));
        return await pending;
    """)
    assert result == {"status": 0, "aborted": False}


def test_download_filename_cannot_be_a_path_and_requests_are_private(javascript):
    result = javascript("""
        let sent;
        const api = new BeeSmartApi({ fetchImpl: async (url, options) => {
            sent = options;
            return response({}, 200, { 'Content-Disposition': "attachment; filename*=UTF-8''..%2F..%2Freport.json" });
        }});
        const file = await api.downloadRun('run-1', 'json');
        return { filename: file.filename, credentials: sent.credentials, cache: sent.cache, redirect: sent.redirect };
    """)
    assert result == {"filename": "report.json", "credentials": "omit", "cache": "no-store", "redirect": "error"}


@pytest.mark.parametrize("replacement", [
    {"name": "wrong.txt", "size": 100},
    {"name": "empty.csv", "size": 0},
    {"name": "large.csv", "size": 32 * 1024 ** 2 + 1},
])
def test_invalid_file_replacement_cannot_submit_previously_selected_file(javascript, replacement):
    # Load actual application functions; DOM doubles provide only the platform
    # methods they call, with no copy of file-validation or button-state logic.
    module = APP_FUNCTIONS + "\nreturn { state, setFile };"
    result = javascript(DOM_DOUBLES + f"""
        const {{ state, setFile }} = new Function({json.dumps(module)})();
        state.connected = true;
        state.overview = {{ runtime: {{ missing_files: [] }}, agent: {{ ready: true }} }};
        for (const role of ['profile', 'history', 'tariffs']) setFile(role, {{ name: role + '.csv', size: 100 }});
        const initiallyEnabled = !document.getElementById('start-button').disabled;
        setFile('profile', {json.dumps(replacement)});
        return {{ initiallyEnabled, remaining: Object.keys(state.files),
            disabled: document.getElementById('start-button').disabled,
            errorFocused: document.activeElement === document.getElementById('form-error'),
            invalid: document.getElementById('file-profile').getAttribute('aria-invalid'),
            filename: document.querySelector('[data-role="profile"]').querySelector('[data-file-name]').textContent }};
    """)
    assert result == {"initiallyEnabled": True, "remaining": ["history", "tariffs"], "disabled": True, "errorFocused": True, "invalid": "true", "filename": "Выбрать CSV"}


def test_connection_reset_clears_private_results_even_when_next_login_fails(javascript):
    module = APP_FUNCTIONS + "\nreturn { state, renderLLM, clearPrivateView };"
    result = javascript(DOM_DOUBLES + f"""
        const {{ state, renderLLM, clearPrivateView }} = new Function({json.dumps(module)})();
        state.connected = true;
        state.overview = {{ dataset: {{ customers: 123 }} }};
        state.run = {{ id: 'private-run', status: 'completed', llm: {{ provider: 'openai', status: 'completed', summary: 'private-explanation' }} }};
        renderLLM();
        document.getElementById('bundled-files').append('private-file');
        clearPrivateView();
        return {{ run: state.run, overview: state.overview, connected: state.connected,
            summary: document.getElementById('llm-summary').textContent,
            metric: document.getElementById('metric-net').textContent,
            files: document.getElementById('bundled-files').children.length,
            disabled: document.getElementById('start-button').disabled }};
    """)
    assert result == {"run": None, "overview": None, "connected": False, "summary": "", "metric": "—", "files": 0, "disabled": True}
