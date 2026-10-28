/* Lounge: a home screen for a 2012 Samsung Smart TV (Orsay).
 * ES5 only. No classList, dataset, Array.prototype.find or arrow functions:
 * the TV's WebKit predates them. Runs unchanged in a desktop browser for preview.
 */
var Lounge = (function () {
	"use strict";

	var CONFIG = window.LOUNGE_CONFIG || {};

	var WALLS = [
		{ file: "wall1.jpg", thumb: "thumb1.jpg", credit: "Alexey Topolyanskiy" },
		{ file: "wall2.jpg", thumb: "thumb2.jpg", credit: "Andrew Ridley" },
		{ file: "wall3.jpg", thumb: "thumb3.jpg", credit: "Wolfgang Lutz" },
		{ file: "wall4.jpg", thumb: "thumb4.jpg", credit: "Andrew Coelho" },
		{ file: "wall5.jpg", thumb: "thumb5.jpg", credit: "Christian Joudrey" },
		{ file: "wall6.jpg", thumb: "thumb6.jpg", credit: "Steve Carter" },
		{ file: "wall7.jpg", thumb: "thumb7.jpg", credit: "Joseph Barrientos" },
		{ file: "wall8.jpg", thumb: "thumb8.jpg", credit: "Kevin Young" }
	];
	var THEMES = [
		{ id: "jarvis", label: "Jarvis" },
		{ id: "lounge", label: "Photo" }
	];
	var FRAME_STEPS = [
		{ minutes: 1, label: "Every minute" },
		{ minutes: 5, label: "Every 5 minutes" },
		{ minutes: 15, label: "Every 15 minutes" },
		{ minutes: 60, label: "Every hour" },
		{ minutes: 0, label: "Never" }
	];
	// Some 2012 TVs show apps at 960x540 while reporting 1280x720, so this is a setting.
	var SCREEN_STEPS = [1, 0.95, 0.9, 0.85, 0.8, 0.75, 0.7];
	var TV_DEFAULT_SCREEN = 0.75;
	var DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
	var MONTHS = ["January", "February", "March", "April", "May", "June", "July",
		"August", "September", "October", "November", "December"];

	var tv = null;        // Samsung APIs when running on the TV
	var keys = {};        // keyCode -> action name
	var prefs = { theme: "jarvis", wall: 0, clock24: false, frameStep: 1, screen: null, tzSec: null };
	var mode = "home";    // home | settings | frame
	var dockIndex = 0;
	var rowIndex = 0;
	var bgFront = null;
	var bgBack = null;
	var frameTimer = null;
	var toastTimer = null;
	var lastMinute = -1;
	var startedAt = new Date().getTime();
	var viewport = { w: 0, h: 0 };

	// Clock source: offset from the device clock, and the UTC offset to display in.
	// The TV's own clock can be unset (it reports 1970), so we sync from the network.
	var time = { known: false, skewMs: 0, tzSec: 0, fixed: null, source: "none" };

	// Jarvis state
	var stats = null;          // last /stats from the Mac, or null
	var statsOk = false;
	var internetOk = null;
	var statsTimer = null;
	var logLines = [];

	// ---------------------------------------------------------------- helpers

	function $(id) { return document.getElementById(id); }

	function hasClass(el, c) { return (" " + el.className + " ").indexOf(" " + c + " ") >= 0; }
	function addClass(el, c) { if (!hasClass(el, c)) { el.className = (el.className ? el.className + " " : "") + c; } }
	function removeClass(el, c) {
		el.className = (" " + el.className + " ").replace(" " + c + " ", " ").replace(/^\s+|\s+$/g, "");
	}
	function setClass(el, c, on) { if (on) { addClass(el, c); } else { removeClass(el, c); } }

	function byClass(root, c) {
		var all = root.getElementsByTagName("*");
		var out = [];
		for (var i = 0; i < all.length; i++) { if (hasClass(all[i], c)) { out.push(all[i]); } }
		return out;
	}

	function pad(n) { return n < 10 ? "0" + n : "" + n; }

	function esc(s) {
		return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
	}

	function isJarvis() { return prefs.theme === "jarvis"; }

	function toast(msg) {
		var el = $("toast");
		el.innerHTML = msg;
		addClass(el, "show");
		if (toastTimer) { clearTimeout(toastTimer); }
		toastTimer = setTimeout(function () { removeClass(el, "show"); }, 2600);
	}

	function request(method, url, timeoutMs, done) {
		var finished = false;
		var xhr = new XMLHttpRequest();
		var timer = setTimeout(function () {
			if (finished) { return; }
			finished = true;
			try { xhr.abort(); } catch (e) {}
			done(null);
		}, timeoutMs);
		xhr.onreadystatechange = function () {
			if (xhr.readyState !== 4 || finished) { return; }
			finished = true;
			clearTimeout(timer);
			done(xhr.status >= 200 && xhr.status < 300 ? xhr : null);
		};
		try {
			xhr.open(method, url, true);
			xhr.send(null);
		} catch (e) {
			finished = true;
			clearTimeout(timer);
			done(null);
		}
	}

	function getJSON(url, timeoutMs, ok, fail) {
		request("GET", url, timeoutMs, function (xhr) {
			var data = null;
			if (xhr) { try { data = JSON.parse(xhr.responseText); } catch (e) {} }
			if (data) { ok(data); } else { fail(); }
		});
	}

	// ---------------------------------------------------------------- storage

	var Store = {
		key: "lounge.prefs",
		dir: "lounge",
		load: function () {
			var raw = null;
			try { raw = window.localStorage && localStorage.getItem(this.key); } catch (e) {}
			if (!raw && tv && tv.fs) {
				try {
					var f = tv.fs.openCommonFile(this.dir + "/prefs.json", "r");
					if (f) { raw = f.readAll(); tv.fs.closeCommonFile(f); }
				} catch (e2) {}
			}
			if (!raw) { return; }
			try {
				var saved = JSON.parse(raw);
				for (var k in saved) { if (prefs.hasOwnProperty(k)) { prefs[k] = saved[k]; } }
			} catch (e3) {}
		},
		save: function () {
			var raw = JSON.stringify(prefs);
			try { if (window.localStorage) { localStorage.setItem(this.key, raw); } } catch (e) {}
			if (tv && tv.fs) {
				try {
					if (!tv.fs.isValidCommonPath(this.dir)) { tv.fs.createCommonDir(this.dir); }
					var f = tv.fs.openCommonFile(this.dir + "/prefs.json", "w");
					f.writeAll(raw);
					tv.fs.closeCommonFile(f);
				} catch (e2) {}
			}
		}
	};

	// ---------------------------------------------------------------- time

	function setTime(utcMs, tzSec, source) {
		var first = !time.known;
		time.skewMs = utcMs - new Date().getTime();
		time.tzSec = tzSec;
		time.known = true;
		time.source = source;
		if (prefs.tzSec !== tzSec) { prefs.tzSec = tzSec; Store.save(); }
		renderClock(true);
		if (first) { log("Clock synchronised via " + source); }
	}

	// 1. The Mac (time + zone). 2. Any web server's Date header (UTC) plus the
	// last zone we learned. 3. The TV's own clock, if it has been set.
	function syncTime() {
		if (!CONFIG.server) { syncFromWeb(); return; }
		getJSON(CONFIG.server + "/time", 4000, function (d) {
			setTime(d.epochMs, d.tzOffsetSec, "Mac");
		}, syncFromWeb);
	}

	function syncFromWeb() {
		var tz = prefs.tzSec !== null ? prefs.tzSec : CONFIG.tzOffsetSec;
		if (tz === undefined || tz === null) { useDeviceClock(); return; }
		request("HEAD", "http://www.google.com/generate_204?t=" + new Date().getTime(), 6000, function (xhr) {
			var ms = xhr ? Date.parse(xhr.getResponseHeader("Date") || "") : NaN;
			if (ms > 0) { setTime(ms, tz, "web"); } else { useDeviceClock(); }
		});
	}

	function useDeviceClock() {
		var d = new Date();
		if (d.getFullYear() >= 2024) {
			time.skewMs = 0;
			time.tzSec = -d.getTimezoneOffset() * 60;
			time.known = true;
			time.source = "TV";
		}
		renderClock(true);
	}

	// A Date whose UTC fields hold local wall-clock time.
	function wallClock() {
		if (time.fixed) { return time.fixed; }
		return new Date(new Date().getTime() + time.skewMs + time.tzSec * 1000);
	}

	function greeting(h) {
		return h < 5 ? "Good night" : h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
	}

	function renderClock(force) {
		var have = time.known || time.fixed;
		$("clock").style.visibility = have ? "visible" : "hidden";
		$("jv-clockblock").style.visibility = have ? "visible" : "hidden";
		if (!have) {
			$("date").innerHTML = "";
			$("greeting").innerHTML = "Welcome home";
			$("frameclock").innerHTML = "";
			return;
		}
		var d = wallClock();
		var h = d.getUTCHours();
		var m = d.getUTCMinutes();
		if (!force && m === lastMinute) { return; }
		lastMinute = m;

		var shown = h;
		var ampm = "";
		if (!prefs.clock24) {
			ampm = h < 12 ? "AM" : "PM";
			shown = h % 12 === 0 ? 12 : h % 12;
		}
		var text = (prefs.clock24 ? pad(shown) : shown) + ":" + pad(m);
		$("time").innerHTML = text;
		$("ampm").innerHTML = ampm;
		$("date").innerHTML = DAYS[d.getUTCDay()] + " " + d.getUTCDate() + " " + MONTHS[d.getUTCMonth()];
		$("greeting").innerHTML = greeting(h) + (CONFIG.userName ? ", " + esc(CONFIG.userName) : "");
		$("frameclock").innerHTML = text + (ampm ? " <span style=\"font-size:20px\">" + ampm + "</span>" : "");

		$("jv-time").innerHTML = text;
		$("jv-day").innerHTML = d.getUTCDate();
		$("jv-month").innerHTML = MONTHS[d.getUTCMonth()] + " " + d.getUTCFullYear();
		$("jv-weekday").innerHTML = DAYS[d.getUTCDay()];
	}

	// ---------------------------------------------------------------- wallpaper

	function showWall(i, instant) {
		var w = WALLS[i];
		var next = bgBack;
		next.onload = function () {
			next.onload = null;
			if (instant) {
				next.style.webkitTransition = "none";
				bgFront.style.webkitTransition = "none";
			}
			addClass(next, "on");
			removeClass(bgFront, "on");
			bgBack = bgFront;
			bgFront = next;
			if (instant) {
				setTimeout(function () {
					bgFront.style.webkitTransition = "";
					bgBack.style.webkitTransition = "";
				}, 50);
			}
		};
		next.src = "img/" + w.file;
		$("credit").innerHTML = "Photo: " + w.credit + " / Unsplash";
	}

	// ---------------------------------------------------------------- theme

	function applyTheme() {
		setClass(document.body, "theme-jarvis", isJarvis());
		if (isJarvis()) {
			startStats();
			renderJarvis();
		} else {
			stopStats();
		}
	}

	// ---------------------------------------------------------------- jarvis

	function log(msg) {
		var d = wallClock();
		var stamp = (time.known || time.fixed) ? pad(d.getUTCHours()) + ":" + pad(d.getUTCMinutes()) + ":" + pad(d.getUTCSeconds()) : "--:--:--";
		logLines.push("<div><span class=\"t\">" + stamp + "</span>" + esc(msg) + "</div>");
		if (logLines.length > 4) { logLines.shift(); }  // keep clear of the name below
		$("jv-log").innerHTML = logLines.join("");
	}

	// Voice/remote commands handled on the Mac, echoed into the on-screen log.
	var lastEventId = 0;
	function showEvents(list, quiet) {
		for (var i = 0; i < list.length; i++) {
			if (list[i].id > lastEventId) {
				lastEventId = list[i].id;
				if (!quiet) { log(list[i].text); }
			}
		}
	}

	// ---------------------------------------------------------------- briefing

	var lastBriefingId = 0;
	var briefTimers = [];

	function showBriefing(b) {
		if (b.id <= lastBriefingId) { return; }
		lastBriefingId = b.id;
		if (b.ageSec > 30 || !isJarvis()) { return; }  // old news, or wrong theme
		hideBriefing();
		$("jv-brief-title").innerHTML = esc(b.title);
		var html = "";
		for (var i = 0; i < b.lines.length; i++) { html += "<div class=\"line\">" + esc(b.lines[i]) + "</div>"; }
		$("jv-brief-body").innerHTML = html;
		addClass($("jv-brief"), "show");
		var lines = byClass($("jv-brief-body"), "line");
		for (var k = 0; k < lines.length; k++) {
			(function (el, delay) {
				briefTimers.push(setTimeout(function () { addClass(el, "in"); }, delay));
			})(lines[k], 300 + k * 1500);
		}
		briefTimers.push(setTimeout(hideBriefing, 300 + lines.length * 1500 + 20000));
	}

	// JARVIS is talking (on the Mac/phone): animate for as long as the voice lasts.
	var speakTimer = null;
	function speakFor(ms) {
		addClass(document.body, "speaking");
		if (speakTimer) { clearTimeout(speakTimer); }
		speakTimer = setTimeout(function () { removeClass(document.body, "speaking"); speakTimer = null; }, ms);
	}

	function briefingVisible() { return hasClass($("jv-brief"), "show"); }

	function hideBriefing() {
		for (var i = 0; i < briefTimers.length; i++) { clearTimeout(briefTimers[i]); }
		briefTimers = [];
		removeClass($("jv-brief"), "show");
	}

	function startStats() {
		if (statsTimer || !CONFIG.server) { return; }
		pollStats();
		statsTimer = setInterval(pollStats, 2000);
	}

	function stopStats() {
		if (statsTimer) { clearInterval(statsTimer); statsTimer = null; }
	}

	function pollStats() {
		getJSON(CONFIG.server + "/stats", 2500, function (d) {
			var wasOk = statsOk;
			stats = d;
			statsOk = true;
			if (!wasOk) { log("Link to " + (d.host || "Mac") + " established"); }
			showEvents(d.events || [], !wasOk);
			if (d.briefing) { showBriefing(d.briefing); }
			if (d.speakingMs > 300) { speakFor(d.speakingMs); }
			renderJarvis();
		}, function () {
			if (statsOk) { log("Link to Mac lost"); }
			statsOk = false;
			renderJarvis();
		});
	}

	// An image load needs no CORS, so this works the same in the preview and on the TV.
	function checkInternet() {
		var img = new Image();
		var settled = false;
		function done(ok) {
			if (settled) { return; }
			settled = true;
			img.onload = img.onerror = null;
			if (ok !== internetOk) { log(ok ? "Internet connection online" : "Internet connection offline"); }
			internetOk = ok;
			renderJarvis();
		}
		img.onload = function () { done(true); };
		img.onerror = function () { done(false); };
		setTimeout(function () { done(false); }, 8000);
		img.src = "http://www.google.com/favicon.ico?t=" + new Date().getTime();
	}

	function duration(sec) {
		if (sec === null || sec === undefined) { return "?"; }
		var d = Math.floor(sec / 86400);
		var h = Math.floor(sec % 86400 / 3600);
		var m = Math.floor(sec % 3600 / 60);
		if (d > 0) { return d + "d " + h + "h " + m + "m"; }
		return pad(h) + ":" + pad(m) + ":" + pad(Math.floor(sec % 60));
	}

	function rate(bps) {
		if (bps === null || bps === undefined) { return "?"; }
		if (bps > 1048576) { return (bps / 1048576).toFixed(1) + " MB/s"; }
		if (bps > 1024) { return Math.round(bps / 1024) + " KB/s"; }
		return Math.round(bps) + " B/s";
	}

	function loadText(load) {
		var out = [];
		for (var i = 0; i < (load || []).length; i++) { out.push(load[i].toFixed(2)); }
		return out.join(" ");
	}

	// One tree row: "[ value ] label -─"
	function row(value, label, cls) {
		var v = value === null || value === undefined ? "" : "<span class=\"b\">[</span> <span class=\"v\">" + esc(value) + "</span> <span class=\"b\">]</span> ";
		return "<div class=\"r " + (cls || "") + "\">" + v + esc(label) + "</div>";
	}
	function head(label) { return "<div class=\"r h\">" + esc(label) + "</div>"; }
	function group(inner) { return "<div class=\"g\">" + inner + "</div>"; }

	function renderJarvis() {
		if (!isJarvis()) { return; }
		var sess = Math.floor((new Date().getTime() - startedAt) / 1000);
		var html = "";

		html += head(CONFIG.tvName || "TV");
		html += group(
			row(CONFIG.tvModel || "Samsung", "model") +
			row(viewport.w + "x" + viewport.h + " @" + Math.round(currentScale() * 100) + "%", "display") +
			row(duration(sess), "session") +
			row(time.source, "clock")
		);

		var s = stats || {};
		var off = !statsOk;
		var warming = !off && s.ready === false;  // server just started, first sample pending
		html += head(off ? "mac" : (s.host || "mac"));
		if (off) {
			html += group(row("offline", "link", "off"));
		} else if (warming) {
			html += group(row("sampling", "status"));
		} else {
			var top = "";
			var list = s.top || [];
			for (var i = 0; i < list.length && i < 4; i++) {
				top += row(list[i].name + "  " + list[i].cpu, "", "hot");
			}
			var mem = s.mem || {};
			var bat = s.battery;
			html += group(
				head("cpu") +
				group(
					row(s.cpu + "%", "used", "hot") +
					row(loadText(s.load), "load") +
					top
				) +
				head("ram") +
				group(
					row(mem.pct + "%", "used") +
					row(mem.usedGB + "/" + mem.totalGB + " GB", "")
				) +
				row(bat ? bat.pct + "% " + bat.state : "n/a", "battery") +
				row(duration(s.uptimeSec), "uptime")
			);
		}

		if (!off && s.xbox) {
			html += head("xbox");
			html += group(row(s.xbox.online ? "online" : "asleep", "console HDMI 3", s.xbox.online ? "" : "off"));
		}

		html += head("net");
		html += group(
			row(internetOk === null ? "checking" : internetOk ? "online" : "offline", "internet", internetOk === false ? "off" : "") +
			(off || warming ? "" : row(rate(s.rxBps), "down") + row(rate(s.txBps), "up"))
		);
		$("jv-tree").innerHTML = html;

		var status;
		if (off) {
			status = "<span class=\"k\">MAC</span><span class=\"off\">OFFLINE</span>";
		} else if (warming) {
			status = "<span class=\"k\">MAC</span>SYNCING";
		} else {
			status = "<span class=\"k\">CPU</span>" + s.cpu + "%" +
				"<span class=\"k\">RAM</span>" + (s.mem ? s.mem.usedGB + "/" + s.mem.totalGB + "GB" : "?") +
				"<span class=\"k\">BAT</span>" + (s.battery ? s.battery.pct + "%" : "n/a");
		}
		$("jv-status").innerHTML = status;
	}

	// ---------------------------------------------------------------- dock

	function tiles() { return byClass($("dock"), "tile"); }

	function focusDock(i) {
		var t = tiles();
		dockIndex = (i + t.length) % t.length;
		for (var k = 0; k < t.length; k++) { setClass(t[k], "focus", k === dockIndex); }
	}

	function activate(action) {
		if (action === "tv") {
			if (tv) { tv.widget.sendExitEvent(); } else { toast("On the TV this goes to live TV"); }
		} else if (action === "hub") {
			if (tv) { tv.widget.sendReturnEvent(); } else { toast("On the TV this opens Smart Hub"); }
		} else if (action === "frame") {
			enterFrame();
		} else if (action === "settings") {
			openSettings();
		}
	}

	// ---------------------------------------------------------------- settings

	// Visible rows only: Wallpaper and Frame don't apply to the Jarvis theme.
	function rows() {
		var all = byClass($("settings"), "row");
		var out = [];
		for (var i = 0; i < all.length; i++) { if (!hasClass(all[i], "hidden")) { out.push(all[i]); } }
		return out;
	}

	function themeIndex() {
		for (var i = 0; i < THEMES.length; i++) { if (THEMES[i].id === prefs.theme) { return i; } }
		return 0;
	}

	function renderSettings() {
		var all = byClass($("settings"), "row");
		for (var i = 0; i < all.length; i++) {
			var name = all[i].getAttribute("data-setting");
			setClass(all[i], "hidden", isJarvis() ? (name === "wall" || name === "frame") : name === "diag");
		}
		var w = WALLS[prefs.wall];
		$("theme-value").innerHTML = THEMES[themeIndex()].label;
		$("wall-thumb").src = "img/" + w.thumb;
		$("wall-value").innerHTML = (prefs.wall + 1) + " of " + WALLS.length;
		$("clock-value").innerHTML = prefs.clock24 ? "24-hour" : "12-hour";
		$("frame-value").innerHTML = FRAME_STEPS[prefs.frameStep].label;
		$("screen-value").innerHTML = Math.round(currentScale() * 100) + "%";
		var r = rows();
		if (rowIndex >= r.length) { rowIndex = r.length - 1; }
		for (var k = 0; k < all.length; k++) { removeClass(all[k], "focus"); }
		addClass(r[rowIndex], "focus");
	}

	function openSettings() {
		mode = "settings";
		rowIndex = 0;
		renderSettings();
		addClass($("settings"), "open");
	}

	function closeSettings() {
		mode = "home";
		removeClass($("settings"), "open");
		Store.save();
	}

	function stepIn(list, current, dir) {
		var i = 0;
		var best = 1e9;
		for (var k = 0; k < list.length; k++) {
			var d = Math.abs(list[k] - current);
			if (d < best) { best = d; i = k; }
		}
		return list[Math.min(list.length - 1, Math.max(0, i + dir))];
	}

	var diagRunning = false;

	function runDiagnostics() {
		if (diagRunning || !window.LoungeDiag) { return; }
		diagRunning = true;
		$("diag-value").innerHTML = "Running...";
		log("Running diagnostics");
		LoungeDiag.run(tv, report, function (ok, total) {
			diagRunning = false;
			$("diag-value").innerHTML = ok + " of " + total + " checks passed";
			log("Diagnostics complete: " + ok + "/" + total);
		});
	}

	function changeSetting(dir) {
		var name = rows()[rowIndex].getAttribute("data-setting");
		if (name === "theme") {
			prefs.theme = THEMES[(themeIndex() + dir + THEMES.length) % THEMES.length].id;
			applyTheme();
		} else if (name === "wall") {
			prefs.wall = (prefs.wall + dir + WALLS.length) % WALLS.length;
			showWall(prefs.wall);
		} else if (name === "clock") {
			prefs.clock24 = !prefs.clock24;
			renderClock(true);
		} else if (name === "frame") {
			prefs.frameStep = (prefs.frameStep + dir + FRAME_STEPS.length) % FRAME_STEPS.length;
		} else if (name === "screen") {
			// Right = bigger, left = smaller
			prefs.screen = stepIn(SCREEN_STEPS, currentScale(), -dir);
			fitScreen();
		}
		renderSettings();
	}

	// ---------------------------------------------------------------- frame mode

	function enterFrame() {
		mode = "frame";
		addClass(document.body, "frame");
		if (tv && tv.plugin) { try { tv.plugin.setOffScreenSaver(); } catch (e) {} }
		var minutes = FRAME_STEPS[prefs.frameStep].minutes;
		if (minutes > 0 && !isJarvis()) {
			frameTimer = setInterval(function () {
				prefs.wall = (prefs.wall + 1) % WALLS.length;
				showWall(prefs.wall);
			}, minutes * 60000);
		}
	}

	function leaveFrame() {
		mode = "home";
		removeClass(document.body, "frame");
		if (frameTimer) { clearInterval(frameTimer); frameTimer = null; }
		if (tv && tv.plugin) { try { tv.plugin.setOnScreenSaver(); } catch (e) {} }
		Store.save();
	}

	// ---------------------------------------------------------------- input

	function buildKeys() {
		var k = {};
		if (tv) {
			var t = tv.keys;
			k[t.KEY_LEFT] = "left";
			k[t.KEY_RIGHT] = "right";
			k[t.KEY_UP] = "up";
			k[t.KEY_DOWN] = "down";
			k[t.KEY_ENTER] = "enter";
			k[t.KEY_RETURN] = "back";
			if (t.KEY_EXIT !== undefined) { k[t.KEY_EXIT] = "exit"; }
		} else {
			k[37] = "left"; k[39] = "right"; k[38] = "up"; k[40] = "down";
			k[13] = "enter"; k[27] = "back"; k[8] = "back";
		}
		return k;
	}

	// Stop the TV's default handling (Return would leave the app).
	function swallow(e) {
		if (tv) { try { tv.widget.blockNavigation(e); } catch (err) {} }
		if (e.preventDefault) { e.preventDefault(); }
	}

	function onKey(e) {
		e = e || window.event;
		var action = keys[e.keyCode];
		if (!action) { return; }
		report("key", { code: e.keyCode, action: action, mode: mode });

		// Any button just closes the briefing first.
		if (briefingVisible() && action !== "exit") {
			swallow(e);
			hideBriefing();
			return;
		}

		// Orsay doesn't leave a full-screen app by itself: we have to ask.
		if (action === "exit") {
			swallow(e);
			if (tv) { tv.widget.sendExitEvent(); }
			return;
		}

		if (mode === "frame") {
			swallow(e);
			leaveFrame();
			return;
		}

		if (mode === "settings") {
			swallow(e);
			var r = rows();
			if (action === "up") { rowIndex = Math.max(0, rowIndex - 1); renderSettings(); }
			else if (action === "down") { rowIndex = Math.min(r.length - 1, rowIndex + 1); renderSettings(); }
			else if (action === "left") { changeSetting(-1); }
			else if (action === "right") { changeSetting(1); }
			else if (action === "enter") {
				var which = r[rowIndex].getAttribute("data-setting");
				if (which === "done") { closeSettings(); }
				else if (which === "diag") { runDiagnostics(); }
				else { changeSetting(1); }
			}
			else if (action === "back") { closeSettings(); }
			return;
		}

		swallow(e);
		if (action === "back") {
			if (tv) { tv.widget.sendReturnEvent(); } else { toast("On the TV this returns to Smart Hub"); }
			return;
		}
		if (action === "left") { focusDock(dockIndex - 1); }
		else if (action === "right") { focusDock(dockIndex + 1); }
		else if (action === "enter") { activate(tiles()[dockIndex].getAttribute("data-action")); }
	}

	// ---------------------------------------------------------------- screen

	function currentScale() {
		if (prefs.screen) { return prefs.screen; }
		if (tv) { return TV_DEFAULT_SCREEN; }
		// Preview: fill the browser window
		return Math.min(viewport.w / 1280, viewport.h / 720) || 1;
	}

	// The layout is authored at 1280x720 inside #stage; zoom it to the chosen size,
	// anchored top-left (where a 960x540 app window starts).
	function fitScreen() {
		viewport.w = window.innerWidth || document.documentElement.clientWidth || 1280;
		viewport.h = window.innerHeight || document.documentElement.clientHeight || 720;
		$("stage").style.zoom = currentScale();
	}

	// Fire-and-forget beacon to the Mac's server log, for debugging on the TV.
	function report(event, data) {
		if (!CONFIG.server || !tv) { return; }
		var q = "event=" + encodeURIComponent(event);
		for (var k in data) { q += "&" + k + "=" + encodeURIComponent(data[k]); }
		new Image().src = CONFIG.server + "/log?" + q + "&t=" + new Date().getTime();
	}

	// ---------------------------------------------------------------- debug state for previews
	// e.g. index.html#time=2026-10-27T19:41&mode=settings&row=1&focus=2&wall=4&theme=lounge

	function applyHash() {
		var h = (location.hash || "").replace(/^#/, "");
		if (!h) { return null; }
		var parts = h.split("&");
		var q = {};
		for (var i = 0; i < parts.length; i++) {
			var kv = parts[i].split("=");
			q[kv[0]] = decodeURIComponent(kv[1] || "");
		}
		if (q.time) {
			var m = q.time.match(/^(\d+)-(\d+)-(\d+)T(\d+):(\d+)$/);
			if (m) { time.fixed = new Date(Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5])); time.source = "fixed"; }
		}
		if (q.wall) { prefs.wall = (+q.wall - 1) % WALLS.length; }
		if (q.clock === "24") { prefs.clock24 = true; }
		if (q.focus) { dockIndex = +q.focus; }
		if (q.theme) { prefs.theme = q.theme; }
		if (q.screen) { prefs.screen = +q.screen; }
		return q;
	}

	// ---------------------------------------------------------------- start

	function start() {
		if (typeof Common !== "undefined" && Common.API && Common.API.Widget) {
			tv = {
				widget: new Common.API.Widget(),
				keys: new Common.API.TVKeyValue(),
				plugin: Common.API.Plugin ? new Common.API.Plugin() : null,
				fs: typeof FileSystem !== "undefined" ? new FileSystem() : null
			};
		}
		keys = buildKeys();
		Store.load();
		if (prefs.wall >= WALLS.length) { prefs.wall = 0; }
		if (prefs.frameStep >= FRAME_STEPS.length) { prefs.frameStep = 1; }

		var q = applyHash() || {};
		fitScreen();
		report("screen", { w: viewport.w, h: viewport.h, sw: screen.width, sh: screen.height, scale: currentScale() });
		window.onresize = function () { if (!tv) { fitScreen(); } };

		bgFront = $("bg-a");
		bgBack = $("bg-b");
		showWall(prefs.wall, true);
		focusDock(dockIndex);

		log("JARVIS " + (CONFIG.version || "") + " online");
		if (CONFIG.userName) { log("Welcome back, " + CONFIG.userName + "."); }
		renderClock(true);
		applyTheme();

		var anchor = $("anchor");
		if (tv) {
			anchor.onkeydown = onKey;
			anchor.focus();
			// Orsay only delivers remote keys to the focused element. If anything steals
			// focus the app stops responding (even to Return), so keep taking it back.
			anchor.onblur = function () {
				report("focus", { lost: 1 });
				setTimeout(function () { anchor.focus(); }, 0);
			};
			setInterval(function () {
				if (document.activeElement !== anchor) { anchor.focus(); }
			}, 1000);
			tv.widget.sendReadyEvent();
		} else {
			document.onkeydown = onKey;
		}

		if (!time.fixed) {
			syncTime();
			setInterval(syncTime, 3600000);
		}
		checkInternet();
		setInterval(checkInternet, 60000);
		setInterval(function () { renderClock(false); }, 1000);
		setInterval(function () { if (!statsTimer) { renderJarvis(); } }, 1000);

		if (q.mode === "settings") { openSettings(); rowIndex = +(q.row || 0); renderSettings(); }
		if (q.mode === "frame") { enterFrame(); }
		if (q.speak) { speakFor(600000); }
	}

	return { start: start };
})();
