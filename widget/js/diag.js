/* Capability check for a 2012 Samsung (Orsay) TV.
 * Read-only: it creates Samsung plugin objects and calls only getters, then
 * reports each result to the Mac's server log. Nothing on the TV is changed.
 */
var LoungeDiag = (function () {
	"use strict";

	// Plugin objects and the read-only calls to try on each.
	var PLUGINS = [
		{ id: "NNAVI", calls: [["GetFirmware"], ["GetSystemVersion", 0], ["GetDUID", ""]] },
		{ id: "TV", calls: [["GetProductCode", 1], ["GetProductType"], ["GetCountry"], ["GetLanguage"]] },
		{ id: "AUDIO", calls: [["GetVolume"], ["GetUserMute"], ["GetOutputDevice"]] },
		{ id: "TVMW", calls: [["GetSource"], ["GetActiveApp"]] },
		{ id: "WINDOW", calls: [["GetSource"], ["GetResolution"]] },
		{ id: "NETWORK", calls: [["GetActiveType"], ["GetIP", 1], ["GetMAC", 1], ["GetIP", 0]] },
		{ id: "TIME", calls: [["GetEpochTime"]] },
		{ id: "SCREEN", calls: [["GetMainScreenSize"]] },
		{ id: "PLAYER", calls: [] },
		{ id: "APPCOMMON", calls: [] },
		{ id: "SEF", calls: [] },
		{ id: "DOWNLOAD", calls: [] },
		{ id: "IMAGEVIEWER", calls: [] }
	];

	// Methods whose existence tells us what later features can do (not called).
	var WATCH = ["SetSource", "SetVolumeWithKey", "SetUserMute", "Play", "Stop", "StartPlayback",
		"Open", "Execute", "RunApp", "LaunchApp", "SetMainScreenSize"];

	function describe(v) {
		if (v === undefined) { return "undefined"; }
		if (v === null) { return "null"; }
		var s = typeof v === "object" ? "[object]" : String(v);
		return s.length > 120 ? s.substring(0, 120) + "..." : s;
	}

	function makePlugin(holder, id) {
		var objId = "diag" + id;
		var div = document.createElement("div");
		div.innerHTML = "<object id=\"" + objId + "\" classid=\"clsid:SAMSUNG-INFOLINK-" + id +
			"\" style=\"position:absolute;width:0;height:0;\"></object>";
		holder.appendChild(div);
		return document.getElementById(objId);
	}

	function listMembers(obj, max) {
		var names = [];
		try {
			for (var k in obj) {
				names.push(k + (typeof obj[k] === "function" ? "()" : ""));
				if (names.length >= max) { break; }
			}
		} catch (e) {
			names.push("(not enumerable: " + e + ")");
		}
		return names.join(",");
	}

	function run(api, report, done) {
		var results = [];
		function note(group, name, ok, value) {
			results.push(ok);
			report("cap", { g: group, n: name, ok: ok ? 1 : 0, v: describe(value) });
		}

		// 1. Samsung JS APIs we already load
		note("env", "userAgent", true, navigator.userAgent);
		note("env", "widgetAPI", !!(api && api.widget), api && api.widget ? listMembers(api.widget, 40) : "");
		note("env", "pluginAPI", !!(api && api.plugin), api && api.plugin ? listMembers(api.plugin, 40) : "");
		note("env", "curWidget", typeof curWidget !== "undefined",
			typeof curWidget !== "undefined" ? "id=" + curWidget.id + " name=" + curWidget.name : "");
		var globals = ["deviceapis", "webapis", "FileSystem", "sf", "localStorage", "JSON", "XMLHttpRequest"];
		for (var g = 0; g < globals.length; g++) {
			var present = false;
			try { present = typeof window[globals[g]] !== "undefined" && window[globals[g]] !== null; } catch (e) {}
			note("global", globals[g], present, present ? typeof window[globals[g]] : "");
		}
		note("api", "runSearchWidget", !!(api && api.widget && api.widget.runSearchWidget), "");

		// 2. Plugin objects, one at a time with a pause so a slow plugin can't stall the UI
		var holder = document.createElement("div");
		holder.style.position = "absolute";
		holder.style.left = "-10px";
		holder.style.top = "-10px";
		document.body.appendChild(holder);

		var i = 0;
		function next() {
			if (i >= PLUGINS.length) {
				var ok = 0;
				for (var r = 0; r < results.length; r++) { if (results[r]) { ok++; } }
				report("cap", { g: "done", n: "summary", ok: 1, v: ok + "/" + results.length });
				done(ok, results.length);
				return;
			}
			var p = PLUGINS[i++];
			var obj = null;
			try { obj = makePlugin(holder, p.id); } catch (e) { note(p.id, "create", false, e); setTimeout(next, 150); return; }
			// An NPAPI object with no backing plugin has no methods at all.
			var alive = false;
			for (var w = 0; w < WATCH.length; w++) { if (obj && typeof obj[WATCH[w]] !== "undefined") { alive = true; } }
			for (var c = 0; c < p.calls.length; c++) { if (obj && typeof obj[p.calls[c][0]] !== "undefined") { alive = true; } }
			note(p.id, "plugin", alive, alive ? "loaded" : "missing");
			if (alive) {
				for (var cc = 0; cc < p.calls.length; cc++) {
					var call = p.calls[cc];
					try {
						var fn = obj[call[0]];
						var val = call.length > 1 ? fn.call(obj, call[1]) : fn.call(obj);
						note(p.id, call[0] + (call.length > 1 ? "(" + call[1] + ")" : "()"), true, val);
					} catch (e2) {
						note(p.id, call[0], false, e2);
					}
				}
				var has = [];
				for (var ww = 0; ww < WATCH.length; ww++) { if (typeof obj[WATCH[ww]] !== "undefined") { has.push(WATCH[ww]); } }
				note(p.id, "methods", has.length > 0, has.join(","));
			}
			setTimeout(next, 150);
		}
		next();
	}

	return { run: run };
})();
