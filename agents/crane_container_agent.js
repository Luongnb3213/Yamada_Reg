// Frida agent for Crane container management.
// Load together with yamada_register_agent.js when you need per-account containers:
//   frida -U -n yamadadenki \
//     -l agents/crane_container_agent.js \
//     -l agents/yamada_register_agent.js \
//     -l agents/current_profile.js
//
// REPL:
//   craneInfo()
//   craneEnsureForProfile()
//   craneSwitch("container-id")

(function craneContainerAgent() {
  const APP_ID = "jp.co.unisys.yamadamobile";
  const NUL = ptr(0);

  function sym(name) {
    return Module.getGlobalExportByName
      ? Module.getGlobalExportByName(name)
      : Module.getExportByName(null, name);
  }

  const objc_lookUpClass = new NativeFunction(sym("objc_lookUpClass"), "pointer", ["pointer"]);
  const sel_registerName = new NativeFunction(sym("sel_registerName"), "pointer", ["pointer"]);
  const msgPtr0 = new NativeFunction(sym("objc_msgSend"), "pointer", ["pointer", "pointer"]);
  const msgPtr1 = new NativeFunction(sym("objc_msgSend"), "pointer", ["pointer", "pointer", "pointer"]);
  const msgPtr2 = new NativeFunction(sym("objc_msgSend"), "pointer", ["pointer", "pointer", "pointer", "pointer"]);
  const msgPtr3Bool = new NativeFunction(sym("objc_msgSend"), "pointer", ["pointer", "pointer", "pointer", "pointer", "bool"]);
  const msgVoid1 = new NativeFunction(sym("objc_msgSend"), "void", ["pointer", "pointer", "pointer"]);
  const msgVoid2 = new NativeFunction(sym("objc_msgSend"), "void", ["pointer", "pointer", "pointer", "pointer"]);
  const msgVoid3 = new NativeFunction(sym("objc_msgSend"), "void", ["pointer", "pointer", "pointer", "pointer", "pointer"]);
  const msgBool1 = new NativeFunction(sym("objc_msgSend"), "bool", ["pointer", "pointer", "pointer"]);
  const msgCount = new NativeFunction(sym("objc_msgSend"), "uint64", ["pointer", "pointer"]);
  const msgIdx = new NativeFunction(sym("objc_msgSend"), "pointer", ["pointer", "pointer", "uint64"]);

  const dlopen = new NativeFunction(sym("dlopen"), "pointer", ["pointer", "int"]);

  function SEL(name) {
    return sel_registerName(Memory.allocUtf8String(name));
  }

  const S_stringWithUTF8String = SEL("stringWithUTF8String:");
  const S_UTF8String = SEL("UTF8String");
  const S_sharedManager = SEL("sharedManager");
  const S_isSupported = SEL("isApplicationSupportedByCrane:");
  const S_activeContainer = SEL("activeContainerIdentifierForApplicationWithIdentifier:");
  const S_setActiveContainer = SEL("setActiveContainerIdentifier:forApplicationWithIdentifier:");
  const S_containerIds = SEL("containerIdentifiersOfApplicationWithIdentifier:");
  const S_createContainer = SEL("createNewContainerWithName:forApplicationWithIdentifier:");
  const S_createContainerWithId = SEL("createNewContainerWithName:andIdentifier:forApplicationWithIdentifier:");
  const S_deleteContainer = SEL("deleteContainerWithIdentifier:forApplicationWithIdentifier:");
  const S_deleteContent = SEL("deleteContentOfContainerWithIdentifier:forApplicationWithIdentifier:");
  const S_wipeContainer = SEL("wipeContainerWithIdentifier:forApplicationWithIdentifier:shouldRepopulate:");
  const S_reloadApp = SEL("reloadApplicationWithIdentifier:");
  const S_displayNameForContainer = SEL("displayNameForContainerWithIdentifier:ofApplicationWithIdentifier:shouldUseShortVersion:");
  const S_count = SEL("count");
  const S_objectAtIndex = SEL("objectAtIndex:");

  let NSString = lookupClass("NSString");
  let managerPtr = NUL;

  function lookupClass(name) {
    return objc_lookUpClass(Memory.allocUtf8String(name));
  }

  function nsstr(value) {
    if (!NSString || NSString.isNull()) NSString = lookupClass("NSString");
    return msgPtr1(NSString, S_stringWithUTF8String, Memory.allocUtf8String(String(value || "")));
  }

  function toStr(ns) {
    if (!ns || ns.isNull()) return "";
    const c = msgPtr0(ns, S_UTF8String);
    return c.isNull() ? "" : c.readUtf8String();
  }

  function arrayToStrings(nsArray) {
    if (!nsArray || nsArray.isNull()) return [];
    const count = Number(msgCount(nsArray, S_count));
    const result = [];
    for (let idx = 0; idx < count; idx++) {
      result.push(toStr(msgIdx(nsArray, S_objectAtIndex, uint64(idx))));
    }
    return result;
  }

  function tryLoadCrane() {
    const paths = [
      "/usr/lib/libcrane.dylib",
      "/var/jb/usr/lib/libcrane.dylib",
      "/usr/lib/libCrane.dylib",
      "/var/jb/usr/lib/libCrane.dylib",
      "/Library/MobileSubstrate/DynamicLibraries/Crane.dylib",
      "/var/jb/Library/MobileSubstrate/DynamicLibraries/Crane.dylib"
    ];
    const loaded = [];
    for (const path of paths) {
      try {
        const handle = dlopen(Memory.allocUtf8String(path), 2);
        if (!handle.isNull()) loaded.push(path);
      } catch (_) {}
    }
    return loaded;
  }

  function manager() {
    if (managerPtr && !managerPtr.isNull()) return managerPtr;
    let cls = lookupClass("CraneManager");
    if (!cls || cls.isNull()) {
      tryLoadCrane();
      cls = lookupClass("CraneManager");
    }
    if (!cls || cls.isNull()) {
      throw new Error("CraneManager class not found. Is Crane/libCrane loaded in this process?");
    }
    managerPtr = msgPtr0(cls, S_sharedManager);
    if (!managerPtr || managerPtr.isNull()) {
      throw new Error("CraneManager sharedManager returned nil.");
    }
    return managerPtr;
  }

  function appId(options) {
    return String((options && options.app_id) || (options && options.application_id) || APP_ID);
  }

  function sanitizeName(value) {
    return String(value || "")
      .replace(/@.*$/, "")
      .replace(/[^A-Za-z0-9_.-]+/g, "_")
      .replace(/^_+|_+$/g, "")
      .slice(0, 48) || "account";
  }

  function defaultContainerName(profile) {
    const base = sanitizeName((profile && (profile.email || profile.nick || profile.nickname)) || "");
    const stamp = new Date().toISOString().replace(/[-:.TZ]/g, "").slice(0, 14);
    return "Yamada_" + base + "_" + stamp;
  }

  function info(options) {
    const applicationId = appId(options);
    const mgr = manager();
    const supported = msgBool1(mgr, S_isSupported, nsstr(applicationId));
    const active = toStr(msgPtr1(mgr, S_activeContainer, nsstr(applicationId)));
    const containers = arrayToStrings(msgPtr1(mgr, S_containerIds, nsstr(applicationId)));
    return {
      ok: true,
      app_id: applicationId,
      supported: supported,
      active_container_id: active,
      containers: containers
    };
  }

  function displayName(containerId, options) {
    const applicationId = appId(options);
    const mgr = manager();
    return toStr(msgPtr3Bool(mgr, S_displayNameForContainer, nsstr(containerId), nsstr(applicationId), true));
  }

  function createContainer(name, options) {
    const applicationId = appId(options);
    const mgr = manager();
    const containerName = String(name || defaultContainerName((options && options.profile) || {}));
    const containerId = toStr(msgPtr2(mgr, S_createContainer, nsstr(containerName), nsstr(applicationId)));
    return {
      ok: !!containerId,
      app_id: applicationId,
      crane_container_id: containerId,
      crane_container_name: containerName,
      active_container_id: toStr(msgPtr1(mgr, S_activeContainer, nsstr(applicationId)))
    };
  }

  function createContainerWithId(name, containerId, options) {
    const applicationId = appId(options);
    const mgr = manager();
    msgVoid3(mgr, S_createContainerWithId, nsstr(name), nsstr(containerId), nsstr(applicationId));
    return {
      ok: true,
      app_id: applicationId,
      crane_container_id: String(containerId || ""),
      crane_container_name: String(name || "")
    };
  }

  function switchContainer(containerId, options) {
    const applicationId = appId(options);
    const reload = !options || options.reload !== false;
    const mgr = manager();
    const id = String(containerId || "").trim();
    if (!id) throw new Error("Missing container id.");
    msgVoid2(mgr, S_setActiveContainer, nsstr(id), nsstr(applicationId));
    if (reload) msgVoid1(mgr, S_reloadApp, nsstr(applicationId));
    return {
      ok: true,
      app_id: applicationId,
      crane_container_id: id,
      crane_container_name: displayName(id, { app_id: applicationId }) || "",
      active_container_id: toStr(msgPtr1(mgr, S_activeContainer, nsstr(applicationId))),
      reloaded: reload
    };
  }

  function ensureForProfile(profile, options) {
    const opts = options || {};
    const prof = profile || globalThis.__YAMADA_PROFILE__ || {};
    const applicationId = appId(opts);
    const existing = String(prof.crane_container_id || opts.crane_container_id || "").trim();
    if (existing) {
      return switchContainer(existing, { app_id: applicationId, reload: opts.reload !== false });
    }
    const name = String(
      opts.crane_container_name ||
      prof.crane_container_name ||
      defaultContainerName(prof)
    );
    const created = createContainer(name, { app_id: applicationId, profile: prof });
    if (!created.ok) return created;
    const switched = switchContainer(created.crane_container_id, { app_id: applicationId, reload: opts.reload !== false });
    switched.crane_container_name = name;
    switched.created = true;
    return switched;
  }

  function deleteContainer(containerId, options) {
    const applicationId = appId(options);
    msgVoid2(manager(), S_deleteContainer, nsstr(containerId), nsstr(applicationId));
    return { ok: true, app_id: applicationId, crane_container_id: String(containerId || ""), deleted: true };
  }

  function wipeContainer(containerId, options) {
    const applicationId = appId(options);
    const repopulate = !!(options && options.repopulate);
    const msgVoid2Bool = new NativeFunction(sym("objc_msgSend"), "void", ["pointer", "pointer", "pointer", "pointer", "bool"]);
    msgVoid2Bool(manager(), S_wipeContainer, nsstr(containerId), nsstr(applicationId), repopulate);
    return { ok: true, app_id: applicationId, crane_container_id: String(containerId || ""), wiped: true, repopulate: repopulate };
  }

  function deleteContent(containerId, options) {
    const applicationId = appId(options);
    msgVoid2(manager(), S_deleteContent, nsstr(containerId), nsstr(applicationId));
    return { ok: true, app_id: applicationId, crane_container_id: String(containerId || ""), content_deleted: true };
  }

  function parse(value) {
    if (value == null || value === "") return {};
    return typeof value === "string" ? JSON.parse(value) : value;
  }

  function printResult(result) {
    const text = JSON.stringify(result, null, 2);
    console.log(text);
    return result;
  }

  rpc.exports = Object.assign(rpc.exports || {}, {
    craneinfo: function (optionsJson) {
      return JSON.stringify(info(parse(optionsJson)));
    },
    cranecreate: function (name, optionsJson) {
      return JSON.stringify(createContainer(name, parse(optionsJson)));
    },
    craneswitch: function (containerId, optionsJson) {
      return JSON.stringify(switchContainer(containerId, parse(optionsJson)));
    },
    craneensure: function (profileJson, optionsJson) {
      return JSON.stringify(ensureForProfile(parse(profileJson), parse(optionsJson)));
    },
    cranedelete: function (containerId, optionsJson) {
      return JSON.stringify(deleteContainer(containerId, parse(optionsJson)));
    },
    cranewipe: function (containerId, optionsJson) {
      return JSON.stringify(wipeContainer(containerId, parse(optionsJson)));
    }
  });

  globalThis.craneInfo = (options) => printResult(info(options || {}));
  globalThis.craneList = (options) => printResult(info(options || {}).containers);
  globalThis.craneCreate = (name, options) => printResult(createContainer(name, options || {}));
  globalThis.craneSwitch = (containerId, options) => printResult(switchContainer(containerId, options || {}));
  globalThis.craneEnsureForProfile = (profile, options) => printResult(ensureForProfile(profile || globalThis.__YAMADA_PROFILE__ || {}, options || {}));
  globalThis.craneDelete = (containerId, options) => printResult(deleteContainer(containerId, options || {}));
  globalThis.craneWipe = (containerId, options) => printResult(wipeContainer(containerId, options || {}));
  globalThis.craneDeleteContent = (containerId, options) => printResult(deleteContent(containerId, options || {}));

  try {
    console.log("[crane-agent] loaded", JSON.stringify(info({}), null, 2));
  } catch (err) {
    console.log("[crane-agent] loaded but CraneManager unavailable:", String(err));
  }
})();
