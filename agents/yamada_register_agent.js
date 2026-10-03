// Frida agent for Yamada email-registration DOM automation.
// Inject:
//   frida -U -n yamadadenki -l /Users/macbook/Desktop/FPT/Yamada_Reg/agents/yamada_register_agent.js
//
// REPL examples:
//   setYamadaProfile({email:"a@example.com", pin:"1234", phone:"09012345678", last_name:"山田", first_name:"太郎", last_name_kana:"ヤマダ", first_name_kana:"タロウ", postal_code:"6751234", prefecture:"兵庫県", city:"加西市", address_rest:"北条町1-1", dob:"19900101", gender:"male"})
//   yamadaStep()
//   setYamadaAuthCode("123456")
//   yamadaRun()

const NUL = ptr(0);

function sym(name) {
  return Module.getGlobalExportByName
    ? Module.getGlobalExportByName(name)
    : Module.getExportByName(null, name);
}

const objc_getClass = new NativeFunction(sym("objc_getClass"), "pointer", ["pointer"]);
const sel_registerName = new NativeFunction(sym("sel_registerName"), "pointer", ["pointer"]);
const msgId = new NativeFunction(sym("objc_msgSend"), "pointer", ["pointer", "pointer"]);
const msgIdx = new NativeFunction(sym("objc_msgSend"), "pointer", ["pointer", "pointer", "uint64"]);
const msgCount = new NativeFunction(sym("objc_msgSend"), "uint64", ["pointer", "pointer"]);
const msgKind = new NativeFunction(sym("objc_msgSend"), "bool", ["pointer", "pointer", "pointer"]);
const msgEval = new NativeFunction(sym("objc_msgSend"), "void", ["pointer", "pointer", "pointer", "pointer"]);
const msg1p = new NativeFunction(sym("objc_msgSend"), "pointer", ["pointer", "pointer", "pointer"]);
const dispatch_async_f = new NativeFunction(sym("dispatch_async_f"), "void", ["pointer", "pointer", "pointer"]);
const mainQ = sym("_dispatch_main_q");
const keepAlive = [];

function onMain(fn) {
  const cb = new NativeCallback(function () {
    try {
      fn();
    } catch (err) {
      send({ err: String(err) });
    }
    const idx = keepAlive.indexOf(cb);
    if (idx >= 0) keepAlive.splice(idx, 1);
  }, "void", ["pointer"]);
  keepAlive.push(cb);
  dispatch_async_f(mainQ, NUL, cb);
}

function SEL(name) {
  return sel_registerName(Memory.allocUtf8String(name));
}

const S_shared = SEL("sharedApplication");
const S_windows = SEL("windows");
const S_subviews = SEL("subviews");
const S_count = SEL("count");
const S_objAt = SEL("objectAtIndex:");
const S_isKind = SEL("isKindOfClass:");
const S_utf8 = SEL("UTF8String");
const S_strWith = SEL("stringWithUTF8String:");
const S_eval = SEL("evaluateJavaScript:completionHandler:");

const NSString = objc_getClass(Memory.allocUtf8String("NSString"));
const UIApplication = objc_getClass(Memory.allocUtf8String("UIApplication"));
const WKWebView = objc_getClass(Memory.allocUtf8String("WKWebView"));

function nsstr(value) {
  return msg1p(NSString, S_strWith, Memory.allocUtf8String(value));
}

function toStr(ns) {
  if (ns.isNull()) return null;
  const c = msgId(ns, S_utf8);
  return c.isNull() ? null : c.readUtf8String();
}

function windows() {
  const app = msgId(UIApplication, S_shared);
  const arr = msgId(app, S_windows);
  const count = Number(msgCount(arr, S_count));
  const result = [];
  for (let idx = 0; idx < count; idx++) result.push(msgIdx(arr, S_objAt, uint64(idx)));
  return result;
}

function findWK(view, out) {
  if (view.isNull()) return;
  if (msgKind(view, S_isKind, WKWebView)) out.push(view);
  const subviews = msgId(view, S_subviews);
  const count = Number(msgCount(subviews, S_count));
  for (let idx = 0; idx < count; idx++) findWK(msgIdx(subviews, S_objAt, uint64(idx)), out);
}

function allWK() {
  const found = [];
  for (const win of windows()) findWK(win, found);
  return found;
}

function makeBlock(handler) {
  const invoke = new NativeCallback(function (block, result, error) {
    try {
      handler(result, error);
    } catch (err) {
      send({ err: "completion:" + String(err) });
    }
  }, "void", ["pointer", "pointer", "pointer"]);
  const descriptor = Memory.alloc(16);
  descriptor.writeU64(0);
  descriptor.add(8).writeU64(32);
  const block = Memory.alloc(32);
  block.writePointer(sym("_NSConcreteGlobalBlock"));
  block.add(8).writeU32(1 << 28);
  block.add(12).writeU32(0);
  block.add(16).writePointer(invoke);
  block.add(24).writePointer(descriptor);
  keepAlive.push(invoke, block, descriptor);
  return block;
}

function evalInWebView(idx, code) {
  return new Promise(function (resolve) {
    onMain(function () {
      const webviews = allWK();
      if (idx >= webviews.length) {
        resolve(null);
        return;
      }
      const block = makeBlock(function (result) {
        resolve(toStr(result));
      });
      msgEval(webviews[idx], S_eval, nsstr(code), block);
    });
  });
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function parseObject(value) {
  if (value == null || value === "") return {};
  if (typeof value === "string") return JSON.parse(value);
  return value;
}

function parsePageResult(raw) {
  if (!raw) return { ok: false, state: "no_webview_or_no_result", raw: raw };
  try {
    return JSON.parse(raw);
  } catch (err) {
    return { ok: false, state: "bad_json_result", raw: raw, error: String(err) };
  }
}

function pageProgram(profile, options, mode) {
  const profileJson = JSON.stringify(profile || {});
  const optionsJson = JSON.stringify(options || {});
  const modeJson = JSON.stringify(mode || "step");
  return `
(function () {
  const profileRaw = ${profileJson};
  const options = ${optionsJson};
  const mode = ${modeJson};

  function q(selector, root) {
    return (root || document).querySelector(selector);
  }
  function qa(selector, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(selector));
  }
  function text(el) {
    return (el && (el.innerText || el.textContent) || "").replace(/\\s+/g, " ").trim();
  }
  function val() {
    for (let idx = 0; idx < arguments.length; idx++) {
      const key = arguments[idx];
      const value = profileRaw[key];
      if (value !== undefined && value !== null && String(value).trim() !== "") {
        return String(value).trim();
      }
    }
    return "";
  }
  function normalizeDigits(value) {
    return String(value || "").replace(/[^0-9]/g, "");
  }
  function normalizeDob(value) {
    const raw = String(value || "").trim();
    if (!raw) return "";
    const compact = raw.replace(/[^0-9]/g, "");
    if (compact.length === 8) return compact;
    const parts = raw.match(/^(\\d{1,4})[\\/\\-.](\\d{1,2})[\\/\\-.](\\d{1,4})$/);
    if (!parts) return compact;
    let y = parts[1], m = parts[2], d = parts[3];
    if (y.length !== 4 && d.length === 4) {
      const tmp = y;
      y = d;
      d = tmp;
    }
    return y.padStart(4, "0") + m.padStart(2, "0") + d.padStart(2, "0");
  }
  function normalizeGender(value) {
    const raw = String(value || "").trim().toLowerCase();
    if (!raw) return "";
    if (["1", "m", "male", "man", "nam"].indexOf(raw) >= 0 || raw.indexOf("男") >= 0) return "1";
    if (["2", "f", "female", "woman", "nu", "nữ"].indexOf(raw) >= 0 || raw.indexOf("女") >= 0) return "2";
    return raw;
  }
  function setField(selector, value) {
    const el = q(selector);
    if (!el) return false;
    el.focus && el.focus();
    el.value = String(value || "");
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    el.blur && el.blur();
    return true;
  }
  function setChecked(selector, checked) {
    const el = q(selector);
    if (!el) return false;
    el.checked = !!checked;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return true;
  }
  function clickElement(el) {
    if (!el) return false;
    el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
    return true;
  }
  function submitForm(form) {
    if (!form) return false;
    if (options.submit === false || options.dryRun) return true;
    if (typeof form.requestSubmit === "function") {
      form.requestSubmit();
    } else {
      form.submit();
    }
    return true;
  }
  function submitButton(form, selector) {
    const btn = selector ? q(selector, form) : q('input[type="submit"], button[type="submit"]', form);
    if (btn && !options.dryRun && options.submit !== false) {
      clickElement(btn);
      return true;
    }
    return submitForm(form);
  }
  function setPrefecture(value) {
    const select = q('select[name="prefcode"]');
    if (!select) return false;
    const target = String(value || "").trim();
    if (!target) return false;
    let selected = null;
    for (const option of Array.prototype.slice.call(select.options || [])) {
      const optText = text(option);
      if (option.value === target || optText === target || optText.indexOf(target) >= 0 || target.indexOf(optText) >= 0) {
        selected = option;
        break;
      }
    }
    if (!selected) return false;
    select.value = selected.value;
    select.dispatchEvent(new Event("change", { bubbles: true }));
    return true;
  }
  function result(state, action, extra) {
    return Object.assign({
      ok: true,
      state: state,
      action: action || "",
      title: document.title || "",
      url: location.href
    }, extra || {});
  }
  const SCREEN_DEFS = [
    {
      state: "tracking_location_consent",
      urlIncludes: ["profilepassport.jp"],
      titleIncludes: ["個別情報開示"],
      requiredSelectors: [".btn-close"]
    },
    {
      state: "member_register_top",
      urlIncludes: ["module=authorize", "action=authorize2"],
      titleIncludes: ["会員登録　トップ"],
      requiredSelectors: ['a[href*="module=memberlogin"][href*="action=reg001"]'],
      absentSelectors: ["#chkbox"]
    },
    {
      state: "app_home_unregistered",
      urlIncludes: ["module=authorize", "action=authorize2"],
      titleIncludes: ["ヤマダアプリ"],
      bodyIncludes: ["総保有ポイント", "未登録"]
    },
    {
      state: "app_home_logged_in",
      urlIncludes: ["module=authorize", "action=authorize2"],
      titleIncludes: ["ヤマダアプリ"],
      bodyIncludes: ["会員証", "総保有ポイント", "マイページ"],
      requiredSelectors: [
        'a[href*="module=barcode"][href*="action=bbc001"]',
        'a[href*="module=mypage"][href*="action=mp001"]'
      ]
    },
    {
      state: "terms_consent",
      urlIncludes: ["module=memberlogin", "action=reg001"],
      titleIncludes: ["会員登録　新規登録"],
      requiredSelectors: ["#chkbox", 'form[name="input01"][action*="func=regist"]', "#registBtn"]
    },
    {
      state: "email_register_input",
      urlIncludes: ["module=memberlogin", "func=regist"],
      titleIncludes: ["メールアドレス登録"],
      requiredSelectors: ['input[name="reg_mail_address"]', 'input[name="mail_address_check"]']
    },
    {
      state: "email_register_confirm",
      urlIncludes: ["module=memberlogin", "action=reg002", "func=confirm"],
      titleIncludes: ["メールアドレス登録"],
      bodyIncludes: ["認証コードをお送りします", "送信"],
      requiredSelectors: ['form[action*="action=regauth"] input[name="token"]'],
      absentSelectors: ['input[name="inputcode"]']
    },
    {
      state: "email_already_registered_login",
      urlIncludes: ["module=memberlogin", "action=regauth"],
      titleIncludes: ["メールアドレス登録"],
      bodyIncludes: ["メールアドレス登録エラー", "ご登録済みのメールアドレス", "ログインボタン"],
      requiredSelectors: [
        "body#mainchange",
        'a[href*="module=memberlogin"][href*="action=regauth"][href*="func=cancel"]'
      ]
    },
    {
      state: "unexpected_error_restart",
      titleIncludes: ["ヤマダアプリ"],
      bodyIncludes: ["予期せぬエラー", "最初からやり直してください"],
      requiredSelectors: ['a[href*="module=authorize"][href*="action=authorize2"]']
    },
    {
      state: "temporary_member_registration_in_progress",
      urlIncludes: ["module=changephone", "action=chgauth"],
      titleIncludes: ["ログイン"],
      bodyIncludes: ["仮会員", "会員登録が正常に完了しなかった", "仮会員の退会"],
      requiredSelectors: ["body#mainchange", 'input[value="アプリトップへ"]']
    },
    {
      state: "email_auth_code_input",
      urlIncludes: ["module=memberlogin", "action=regauth"],
      titleIncludes: ["メールアドレス登録"],
      bodyIncludes: ["認証コード"],
      requiredSelectors: ['input[name="inputcode"]']
    },
    {
      state: "member_info_input",
      urlIncludes: ["module=memberlogin", "action=reg003"],
      titleIncludes: ["会員登録"],
      requiredSelectors: [
        'form[name="input01"] input[name="password"]',
        'input[name="tel"]',
        'input[name="sei"]',
        'input[name="mei"]',
        'input[name="seikana"]',
        'input[name="meikana"]',
        'input[name="zip"]',
        'select[name="prefcode"]',
        'input[name="adrs1"]',
        'input[name="adrs2"]'
      ]
    },
    {
      state: "member_info_confirm",
      urlIncludes: ["module=memberlogin", "action=reg005"],
      titleIncludes: ["お客様情報"],
      bodyIncludes: ["上記で登録する"],
      requiredSelectors: [
        'form[name="inputreg"][action*="action=reg005"]',
        'form[name="inputreg"] input[type="submit"][value="上記で登録する"]'
      ]
    },
    {
      state: "member_register_complete",
      urlIncludes: ["module=memberlogin", "action=reg006"],
      titleIncludes: ["会員登録完了"],
      bodyIncludes: ["会員登録が完了しました", "アプリを起動する"],
      requiredSelectors: ['a[href="ymd://"]']
    },
    {
      state: "maybe_complete",
      urlRegex: "(complete|finish|done|reg004|registend)",
      bodyIncludes: ["完了"]
    }
  ];
  function listStatus(selectors, expectedPresent) {
    return (selectors || []).map(function (selector) {
      return { selector: selector, ok: expectedPresent ? !!q(selector) : !q(selector) };
    });
  }
  function screenInfo() {
    const url = location.href || "";
    const titleValue = document.title || "";
    const bodyValue = text(document.body);
    const candidates = SCREEN_DEFS.map(function (def) {
      const required = listStatus(def.requiredSelectors, true);
      const absent = listStatus(def.absentSelectors, false);
      const urlHits = (def.urlIncludes || []).filter(function (hint) { return url.indexOf(hint) >= 0; });
      const titleHits = (def.titleIncludes || []).filter(function (hint) { return titleValue.indexOf(hint) >= 0; });
      const bodyHits = (def.bodyIncludes || []).filter(function (hint) { return bodyValue.indexOf(hint) >= 0; });
      const regexHit = def.urlRegex ? new RegExp(def.urlRegex, "i").test(url) : false;
      const requiredOk = required.filter(function (item) { return item.ok; }).length;
      const absentOk = absent.filter(function (item) { return item.ok; }).length;
      const requiredTotal = required.length;
      const absentTotal = absent.length;
      const score =
        requiredOk * 10 +
        absentOk * 3 +
        urlHits.length * 2 +
        titleHits.length * 3 +
        bodyHits.length * 3 +
        (regexHit ? 2 : 0);
      const hardMissing = required.filter(function (item) { return !item.ok; }).map(function (item) { return item.selector; });
      return {
        state: def.state,
        score: score,
        confidence: requiredTotal ? requiredOk / requiredTotal : (score > 0 ? 1 : 0),
        matchedSelectors: required.filter(function (item) { return item.ok; }).map(function (item) { return item.selector; }),
        missingSelectors: hardMissing,
        absentChecks: absent,
        urlHits: urlHits,
        titleHits: titleHits,
        bodyHits: bodyHits,
        regexHit: regexHit
      };
    }).sort(function (a, b) {
      if (b.score !== a.score) return b.score - a.score;
      return b.confidence - a.confidence;
    });
    const best = candidates[0] || { state: "unknown", score: 0, confidence: 0 };
    const known = best.score > 0 && best.confidence >= 0.5;
    return {
      state: known ? best.state : "unknown",
      confidence: best.confidence || 0,
      score: best.score || 0,
      readyState: document.readyState || "",
      title: titleValue,
      url: url,
      best: best,
      candidates: candidates.slice(0, 5),
      bodyText: bodyValue.slice(0, 500)
    };
  }
  function detect() {
    return screenInfo().state;
  }
  function requireFields(fields) {
    return fields.filter(function (item) { return !item.value; }).map(function (item) { return item.name; });
  }

  const currentScreen = screenInfo();
  let state = currentScreen.state;
  const pageText = text(document.body);
  if (state === "unknown") {
    const looksLikeAuthCode =
      !!q('input[name="inputcode"]') ||
      ((location.href || "").indexOf("action=regauth") >= 0 && pageText.indexOf("認証コード") >= 0);
    if (looksLikeAuthCode) {
      state = "email_auth_code_input";
      currentScreen.state = state;
    }
  }
  if (mode === "detect") return JSON.stringify(result(state, "detect", { screen: currentScreen }));
  if (mode === "screen") return JSON.stringify(currentScreen);

  switch (state) {
    case "tracking_location_consent": {
      if (!options.dryRun && options.submit !== false) {
        if (typeof window.Onclick === "function") window.Onclick();
        else clickElement(q(".btn-close"));
      }
      return JSON.stringify(result(state, "close_tracking_consent"));
    }
    case "member_register_top": {
      const link = q('a[href*="module=memberlogin"][href*="action=reg001"]');
      if (!options.dryRun && options.submit !== false) clickElement(link);
      return JSON.stringify(result(state, "open_new_member_registration"));
    }
    case "app_home_unregistered": {
      return JSON.stringify(result(state, "no_flow_action", {
        ok: false,
        reason: "App home/unregistered screen detected; registration entry selector is not known on this screen yet.",
        screen: currentScreen
      }));
    }
    case "app_home_logged_in": {
      return JSON.stringify(result(state, "already_logged_in_home", {
        already_logged_in: true
      }));
    }
    case "terms_consent": {
      setChecked("#chkbox", true);
      if (typeof window.consentCheck === "function") window.consentCheck();
      const form = document.forms.input01;
      submitForm(form);
      return JSON.stringify(result(state, "accept_terms_and_submit"));
    }
    case "email_register_input": {
      const email = val("email", "mail");
      if (!email) return JSON.stringify(result(state, "missing_data", { ok: false, missing: ["email"] }));
      setField('input[name="reg_mail_address"]', email);
      setField('input[name="mail_address_check"]', email);
      submitButton(q('input[name="reg_mail_address"]').form);
      return JSON.stringify(result(state, "fill_email_and_submit", { email: email }));
    }
    case "email_register_confirm": {
      const form = q('form[action*="action=regauth"]');
      submitButton(form);
      return JSON.stringify(result(state, "send_auth_email"));
    }
    case "email_already_registered_login": {
      return JSON.stringify(result(state, "already_logged_in_home", {
        already_logged_in: true,
        reason: "email_already_registered"
      }));
    }
    case "unexpected_error_restart": {
      const link = q('a[href*="module=authorize"][href*="action=authorize2"]');
      if (!options.dryRun && options.submit !== false) clickElement(link);
      return JSON.stringify(result(state, "restart_after_unexpected_error"));
    }
    case "temporary_member_registration_in_progress": {
      return JSON.stringify(result(state, "fail_no_retry", {
        ok: false,
        failNoRetry: true,
        reason: "đang trong quá trình đăng ký ở máy khác",
        screen: currentScreen
      }));
    }
    case "email_auth_code_input": {
      const authCode = val("auth_code", "authCode", "email_code", "emailCode", "inputcode", "otp");
      if (!authCode) {
        return JSON.stringify(result(state, "need_auth_code", { ok: false, wait: "auth_code" }));
      }
      const input = q('input[name="inputcode"]');
      if (!input) {
        return JSON.stringify(result(state, "missing_auth_code_input", { ok: false, screen: currentScreen }));
      }
      setField('input[name="inputcode"]', authCode);
      submitButton(input.form);
      return JSON.stringify(result(state, "fill_auth_code_and_submit"));
    }
    case "member_info_input": {
      const mapped = {
        pin: normalizeDigits(val("pin")),
        phone: normalizeDigits(val("phone", "tel")),
        lastName: val("last_name", "lastName", "sei"),
        firstName: val("first_name", "firstName", "mei"),
        lastKana: val("last_name_kana", "katakana_last_name", "lastNameKana", "seikana"),
        firstKana: val("first_name_kana", "katakana_first_name", "firstNameKana", "meikana"),
        postal: normalizeDigits(val("postal_code", "postalCode", "zip")),
        prefecture: val("prefecture", "prefcode"),
        city: val("city", "adrs1"),
        addressRest: val("address_rest", "addressRest", "adrs2", "address"),
        dob: normalizeDob(val("dob", "birth_date", "birthday", "birthdate")),
        gender: normalizeGender(val("gender", "sex"))
      };
      const missing = requireFields([
        { name: "pin", value: mapped.pin },
        { name: "phone", value: mapped.phone },
        { name: "last_name", value: mapped.lastName },
        { name: "first_name", value: mapped.firstName },
        { name: "last_name_kana", value: mapped.lastKana },
        { name: "first_name_kana", value: mapped.firstKana },
        { name: "postal_code", value: mapped.postal },
        { name: "prefecture", value: mapped.prefecture },
        { name: "city", value: mapped.city },
        { name: "address_rest", value: mapped.addressRest }
      ]);
      if (missing.length && !options.allowPartial) {
        return JSON.stringify(result(state, "missing_data", { ok: false, missing: missing }));
      }
      setField('input[name="password"]', mapped.pin);
      setField('input[name="tel"]', mapped.phone);
      setField('input[name="sei"]', mapped.lastName);
      setField('input[name="mei"]', mapped.firstName);
      setField('input[name="seikana"]', mapped.lastKana);
      setField('input[name="meikana"]', mapped.firstKana);
      setField('input[name="zip"]', mapped.postal);
      setPrefecture(mapped.prefecture);
      setField('input[name="adrs1"]', mapped.city);
      setField('input[name="adrs2"]', mapped.addressRest);
      if (mapped.dob) setField('input[name="birthday"]', mapped.dob);
      if (mapped.gender) setChecked('input[name="sex"][value="' + mapped.gender + '"]', true);
      submitButton(document.forms.input01);
      return JSON.stringify(result(state, "fill_member_info_and_submit", { filled: mapped }));
    }
    case "member_info_confirm": {
      const form = document.forms.inputreg || q('form[name="inputreg"]');
      submitButton(form);
      return JSON.stringify(result(state, "confirm_member_info_and_register"));
    }
    case "member_register_complete": {
      const link = q('a[href="ymd://"]');
      if (!options.dryRun && options.submit !== false) clickElement(link);
      return JSON.stringify(result(state, "launch_app_after_registration"));
    }
    default:
      return JSON.stringify(result(state, "no_action", { ok: false, screen: currentScreen, bodyText: text(document.body).slice(0, 500) }));
  }
})()
`;
}

let savedProfile = {};
let savedOptions = {};

async function detectInternal(idx) {
  return parsePageResult(await evalInWebView(idx || 0, pageProgram({}, {}, "detect")));
}

async function screenInternal(idx) {
  return parsePageResult(await evalInWebView(idx || 0, pageProgram({}, {}, "screen")));
}

async function stepInternal(idx, profile, options) {
  return parsePageResult(await evalInWebView(idx || 0, pageProgram(profile || {}, options || {}, "step")));
}

async function runInternal(idx, profile, options) {
  const opts = Object.assign({
    maxSteps: 20,
    delayMs: 300,
    pollMs: 500,
    waitTimeoutMs: 15000,
    stablePolls: 2
  }, options || {});
  const history = [];
  for (let step = 0; step < opts.maxSteps; step++) {
    const res = await stepInternal(idx || 0, profile || {}, opts);
    if (!res.ok && res.state === "unknown" && res.action === "no_action") {
      history.push(res);
      const wait = await waitAfterActionInternal(idx || 0, res, Object.assign({}, opts, { noWait: false }));
      if (wait && (opts.includeWaits || !wait.ok)) history.push(wait);
      if (wait && !wait.ok) break;
      continue;
    }
    history.push(res);
    if (!res.ok || res.wait || res.state === "maybe_complete" || res.state === "member_register_complete") break;
    if (res.action === "launch_app_after_registration" || res.action === "already_logged_in_home") break;
    const wait = await waitAfterActionInternal(idx || 0, res, opts);
    if (wait && (opts.includeWaits || !wait.ok)) history.push(wait);
    if (wait && !wait.ok) break;
  }
  return { ok: true, history: history };
}

async function waitAfterActionInternal(idx, previous, options) {
  const opts = Object.assign({ delayMs: 300, pollMs: 500, waitTimeoutMs: 15000, stablePolls: 2 }, options || {});
  if (opts.noWait || opts.submit === false || opts.dryRun) {
    await sleep(opts.delayMs);
    return { ok: true, action: "wait_skipped" };
  }

  const start = Date.now();
  const firstState = previous && previous.state;
  const firstUrl = previous && previous.url;
  const expectsNavigation = [
    "open_new_member_registration",
    "accept_terms_and_submit",
    "fill_email_and_submit",
    "send_auth_email",
    "fill_auth_code_and_submit",
    "fill_member_info_and_submit",
    "confirm_member_info_and_register",
    "restart_after_unexpected_error"
  ].indexOf(previous && previous.action) >= 0;
  let lastKey = "";
  let stableCount = 0;
  let latest = null;

  await sleep(Math.max(0, Number(opts.delayMs || 0)));
  while (Date.now() - start < Number(opts.waitTimeoutMs || 15000)) {
    latest = await screenInternal(idx || 0);
    const key = [latest.state, latest.url, latest.readyState, latest.score].join("|");
    if (key === lastKey) stableCount += 1;
    else {
      lastKey = key;
      stableCount = 1;
    }

    const changed = latest.state !== firstState || latest.url !== firstUrl;
    const ready = !latest.readyState || latest.readyState === "interactive" || latest.readyState === "complete";
    if (changed && ready && stableCount >= Number(opts.stablePolls || 2)) {
      return {
        ok: true,
        action: "wait_screen_changed",
        waitedMs: Date.now() - start,
        from: { state: firstState, url: firstUrl },
        to: { state: latest.state, url: latest.url, readyState: latest.readyState }
      };
    }

    // Some actions update the current page in-place. If the page is stable and
    // ready, move on instead of burning the whole timeout.
    if (!expectsNavigation && !changed && ready && stableCount >= Math.max(3, Number(opts.stablePolls || 2) + 1)) {
      return {
        ok: true,
        action: "wait_screen_stable",
        waitedMs: Date.now() - start,
        state: latest.state,
        url: latest.url,
        readyState: latest.readyState
      };
    }
    await sleep(Number(opts.pollMs || 500));
  }

  return {
    ok: false,
    action: "wait_timeout",
    waitedMs: Date.now() - start,
    from: { state: firstState, url: firstUrl },
    last: latest
  };
}

rpc.exports = {
  count: function () {
    return allWK().length;
  },
  runjs: function (idx, code) {
    return evalInWebView(idx || 0, code);
  },
  dumpdom: function (idx) {
    return evalInWebView(idx || 0, "document.documentElement.outerHTML");
  },
  yamadadetect: function (idx) {
    return detectInternal(idx || 0).then((res) => JSON.stringify(res));
  },
  yamadascreen: function (idx) {
    return screenInternal(idx || 0).then((res) => JSON.stringify(res));
  },
  yamadasetprofile: function (profileJson) {
    savedProfile = parseObject(profileJson);
    return JSON.stringify({ ok: true, profile: savedProfile });
  },
  yamadasetauthcode: function (code) {
    savedProfile.auth_code = String(code || "").trim();
    return JSON.stringify({ ok: true });
  },
  yamadastep: function (idx, profileJson, optionsJson) {
    const profile = Object.assign({}, savedProfile, parseObject(profileJson));
    const options = Object.assign({}, savedOptions, parseObject(optionsJson));
    return stepInternal(idx || 0, profile, options).then((res) => JSON.stringify(res));
  },
  yamadarun: function (idx, profileJson, optionsJson) {
    const profile = Object.assign({}, savedProfile, parseObject(profileJson));
    const options = Object.assign({}, savedOptions, parseObject(optionsJson));
    return runInternal(idx || 0, profile, options).then((res) => JSON.stringify(res));
  }
};

globalThis.runJS = (code, idx) => evalInWebView(idx || 0, code).then((res) => console.log(res) || res);
globalThis.dumpDOM = (idx) => evalInWebView(idx || 0, "document.documentElement.outerHTML")
  .then((res) => console.log("len=" + (res ? res.length : 0)) || res);

globalThis.setYamadaProfile = function (profile) {
  savedProfile = typeof profile === "string" ? JSON.parse(profile) : (profile || {});
  console.log("[yamada-agent] profile set: " + (savedProfile.email || "(no email)"));
  return savedProfile;
};
globalThis.setYamadaAuthCode = function (code) {
  savedProfile.auth_code = String(code || "").trim();
  console.log(JSON.stringify({ ok: true }, null, 2));
};
globalThis.yamadaDetect = (idx) => detectInternal(idx || 0)
  .then((res) => console.log(JSON.stringify(res, null, 2)) || res);
globalThis.yamadaScreen = (idx) => screenInternal(idx || 0)
  .then((res) => console.log(JSON.stringify(res, null, 2)) || res);
globalThis.yamadaStep = (profile, options, idx) => stepInternal(idx || 0, Object.assign({}, savedProfile, profile || {}), Object.assign({}, savedOptions, options || {}))
  .then((res) => console.log(JSON.stringify(res, null, 2)) || res);
globalThis.yamadaRun = (profile, options, idx) => runInternal(idx || 0, Object.assign({}, savedProfile, profile || {}), Object.assign({}, savedOptions, options || {}))
  .then((res) => console.log(JSON.stringify(res, null, 2)) || res);

console.log("[yamada-agent] loaded - WKWebView count:", allWK().length);
