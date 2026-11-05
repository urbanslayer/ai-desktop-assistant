// Riko desktop awareness for GNOME: a tiny read-only window/monitor list
// plus Activate, exported on the session bus as im.riko.Windows so the
// Riko desktop app can offer desktop_windows/desktop_focused/desktop_monitors
// /focus_window under GNOME (docs/DESKTOP_SENSE.md §3).
//
// Install (one command, see README):
//   cp -r desktop/gnome-extension/riko-windows@riko \
//       ~/.local/share/gnome-shell/extensions/
// then restart GNOME Shell (Alt-F2 'r' on X11, log out/in on Wayland) or
// enable it with: gnome-extensions enable riko-windows@riko
import Meta from 'gi://Meta';
import Gio from 'gi://Gio';
import Shell from 'gi://Shell';

const IFACE = 'im.riko.Windows';
const OBJ = '/im/riko/Windows';

const INTROSPECTION = `
<node>
  <interface name="${IFACE}">
    <method name="List">
      <arg name="json" type="s" direction="out"/>
    </method>
    <method name="Monitors">
      <arg name="json" type="s" direction="out"/>
    </method>
    <method name="Activate">
      <arg name="id" type="s" direction="in"/>
      <arg name="json" type="s" direction="out"/>
    </method>
    <method name="ActivateWorkspace">
      <arg name="index" type="s" direction="in"/>
      <arg name="json" type="s" direction="out"/>
    </method>
  </interface>
</node>`;

let _extension = null;
let _ownerId = 0;

function winId(w) {
  return String(w.get_stable_sequence?.() ?? w.get_id());
}

function describeWindows() {
  const focus = global.display.focus_window;
  const workspaces = workspaceInfo();
  const windows = [];
  for (const actor of global.get_window_actors()) {
    const w = actor.get_meta_window();
    if (!w || w.is_hidden()) {
      continue;
    }
    let monitor = '';
    try {
      const idx = w.get_monitor();
      const info = global.display.get_monitor_info(idx);
      monitor = info ? `MVN-${idx}` : `MVN-${idx}`;
      if (info) {
        monitor = String(idx);
      }
    } catch (e) { /* leave blank */ }
    let ws = '';
    try {
      const wks = w.get_workspace();
      ws = wks ? String(wks.index()) : '';
    } catch (e) { /* ignore */ }
    windows.push({
      id: winId(w),
      app: w.get_wm_class() ? w.get_wm_class()[0] : '',
      title: w.get_title() || '',
      workspace: ws,
      monitor,
      focused: focus === w,
      fullscreen: w.get_fullscreen(),
      floating: w.get_window_type() === Meta.WindowType.DIALOG ||
                w.get_window_type() === Meta.WindowType.MODAL_DIALOG,
      pid: w.get_pid() ? w.get_pid() : null,
    });
  }
  const focused = windows.find(x => x.focused) || null;
  return JSON.stringify({ ok: true, windows, focused, workspaces });
}

function describeMonitors() {
  const n = global.display.get_n_monitors();
  let activeWs = '';
  try {
    const wm = global.workspace_manager;
    const idx = wm.get_active_workspace_on_monitor !== undefined
      ? null : null;   // per-monitor lookup differs across releases; the
                       // active workspace index is the safe floor
    activeWs = String(wm.get_workspace_index(wm.get_active_workspace()));
  } catch (e) { /* leave blank */ }
  const monitors = [];
  for (let i = 0; i < n; i++) {
    const info = global.display.get_monitor_info(i);
    if (!info) {
      continue;
    }
    monitors.push({
      name: `MVN-${i}`,
      description: info.get_product() || info.get_vendor() || '',
      size: `${info.get_width()}x${info.get_height()}`,
      position: `${info.get_x_output()}x${info.get_y_output()}`,
      scale: info.get_scale(),
      focused: i === global.display.get_focus_monitor(),
      active_workspace: i === global.display.get_focus_monitor() ? activeWs : '',
    });
  }
  return JSON.stringify({ ok: true, monitors });
}

function workspaceInfo() {
  // 0-based index + configured name per workspace (List consumers need
  // names to resolve 'the web one'-style refs)
  const wm = global.workspace_manager;
  const list = [];
  try {
    const n = wm.get_n_workspaces();
    for (let i = 0; i < n; i++) {
      list.push({index: i,
                 name: wm.get_workspace_name_by_index
                       ? String(wm.get_workspace_name_by_index(i) || '') : ''});
    }
  } catch (e) { /* ignore */ }
  return list;
}

function activateWorkspace(ref) {
  // ref: 0-based index as a string, or a workspace name
  try {
    const wm = global.workspace_manager;
    let idx = parseInt(ref, 10);
    if (Number.isNaN(idx)) {
      idx = -1;
      const want = String(ref).toLowerCase();
      for (let i = 0; i < wm.get_n_workspaces(); i++) {
        const nm = wm.get_workspace_name_by_index
          ? String(wm.get_workspace_name_by_index(i) || '') : '';
        if (nm.toLowerCase() === want) { idx = i; break; }
      }
    }
    const ws = wm.get_workspace_by_index(idx);
    if (!ws) {
      return JSON.stringify({ok: false, error: `no workspace ${ref}`});
    }
    ws.activate(global.get_current_time());
    return JSON.stringify({ok: true, workspace: String(idx)});
  } catch (e) {
    return JSON.stringify({ok: false, error: String(e)});
  }
}

function activate(id) {
  for (const actor of global.get_window_actors()) {
    const w = actor.get_meta_window();
    if (w && winId(w) === id) {
      try {
        w.activate(global.get_current_time());
        return JSON.stringify({ ok: true });
      } catch (e) {
        return JSON.stringify({ ok: false, error: String(e) });
      }
    }
  }
  return JSON.stringify({ ok: false, error: `no window id ${id}` });
}

export function enable(extension) {
  _extension = extension;
  _ownerId = Gio.DBus.session.own_name(IFACE,
    Gio.BusNameOwnerFlags.DO_NOT_QUEUE, null, null);
  Gio.DBus.session.register_object(
    OBJ, INTROSPECTION,
    (sender, objectPath, method, params, invocation) => {
      try {
        if (method === 'List') {
          invocation.return_value(new GLib.Variant('(s)', [describeWindows()]));
        } else if (method === 'Monitors') {
          invocation.return_value(new GLib.Variant('(s)', [describeMonitors()]));
        } else if (method === 'Activate') {
          invocation.return_value(
            new GLib.Variant('(s)', [activate(params.get_child(0).unpack())]));
        } else if (method === 'ActivateWorkspace') {
          invocation.return_value(
            new GLib.Variant('(s)', [activateWorkspace(params.get_child(0).unpack())]));
        } else {
          invocation.return_error_literal(
            Gio.DBusError, Gio.DBusError.UNKNOWN_METHOD, method);
        }
      } catch (e) {
        invocation.return_error_literal(
          Gio.DBusError, Gio.DBusError.FAILED, String(e));
      }
    },
    null);
}

export function disable() {
  if (_ownerId) {
    Gio.DBus.session.unown_name(_ownerId);
    _ownerId = 0;
  }
  _extension = null;
}
