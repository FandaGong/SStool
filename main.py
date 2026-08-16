import sys
import os
import io
import time
import json
import re
import shutil
import tempfile
import threading
import subprocess
import ctypes
import ctypes.util
from ctypes import c_bool, c_void_p, c_char_p, c_long, c_uint32, POINTER

import objc
from PIL import Image
from PyObjCTools import AppHelper
import Quartz

# Native macOS Foundation & AppKit UI Bindings
from Cocoa import (
    NSApp, NSApplication, NSWindow, NSPanel, NSMenu, NSMenuItem, NSStatusBar,
    NSWindowStyleMaskTitled, NSWindowStyleMaskBorderless, NSWindowStyleMaskNonactivatingPanel,
    NSWindowStyleMaskResizable, NSWindowStyleMaskClosable,
    NSMakeRect, NSMakeSize, NSStatusWindowLevel, NSTextView, NSScrollView,
    NSWindowCollectionBehaviorCanJoinAllSpaces, NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSEvent, NSKeyDownMask, NSButton, NSTextField,
    NSApplicationActivationPolicyAccessory
)
import AppKit
from AppKit import (
    NSColor, NSView, NSViewWidthSizable, NSViewHeightSizable,
    NSViewMinXMargin, NSViewMinYMargin,
    NSFont, NSScreen, NSSlider,
    NSColorWell, NSColorSpace
)
from google import genai

# ==============================================================================
# Local Project Directory & Configuration Path
# ==============================================================================

SCRIPT_PATH = os.path.abspath(__file__) if "__file__" in globals() else os.path.abspath(sys.argv[0])
PROJECT_DIR = os.path.dirname(SCRIPT_PATH)
CONFIG_FILE = os.path.join(PROJECT_DIR, "ai_solver_config.json")

MODIFIER_ORDER = ["cmd", "ctrl", "opt", "shift"]
FAINT_TEXT_OPACITY = 0.20
NS_WINDOW_SHARING_NONE = 0

EMOJI_REGEX = re.compile(
    r"[\U00010000-\U0010ffff\u2600-\u27ff\u2300-\u23ff\u2b00-\u2bff\u200d\ufe0f\u2190-\u21ff\u3297\u3299\u3030\u303d]"
)


# ==============================================================================
# Low-Level macOS C-API & Accessibility / Screen Permissions
# ==============================================================================

def check_and_request_accessibility(prompt=True):
    try:
        app_services_name = ctypes.util.find_library("ApplicationServices") or "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
        core_foundation_name = ctypes.util.find_library("CoreFoundation") or "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"

        app_services = ctypes.CDLL(app_services_name)
        core_foundation = ctypes.CDLL(core_foundation_name)

        cfstr_create = core_foundation.CFStringCreateWithCString
        cfstr_create.restype = c_void_p
        cfstr_create.argtypes = [c_void_p, c_char_p, c_uint32]
        k_prompt_key = cfstr_create(None, b"AXTrustedCheckOptionPrompt", 0x08000100)

        k_cf_boolean_true = c_void_p.in_dll(core_foundation, "kCFBooleanTrue")

        cfdict_create = core_foundation.CFDictionaryCreate
        cfdict_create.restype = c_void_p
        keys = (c_void_p * 1)(k_prompt_key)
        values = (c_void_p * 1)(k_cf_boolean_true)
        cfdict_create.argtypes = [c_void_p, POINTER(c_void_p), POINTER(c_void_p), c_long, c_void_p, c_void_p]
        options = cfdict_create(None, keys, values, 1, None, None) if prompt else None

        ax_trusted = app_services.AXIsProcessTrustedWithOptions
        ax_trusted.restype = c_bool
        ax_trusted.argtypes = [c_void_p]
        return bool(ax_trusted(options))
    except Exception:
        return False


def check_and_request_screen_recording():
    """Checks and prompts for macOS Screen Recording permission (macOS 10.15+)."""
    try:
        has_access = Quartz.CGPreflightScreenCaptureAccess()
        if not has_access:
            Quartz.CGRequestScreenCaptureAccess()
            has_access = Quartz.CGPreflightScreenCaptureAccess()
        return bool(has_access)
    except Exception:
        return True


# ==============================================================================
# Native In-Memory Screen Capture Engine (Quartz + PIL)
# ==============================================================================

def capture_screen_to_pil(bounds=None):
    """
    Captures screen directly into an in-memory PIL Image via Quartz.
    Falls back to screencapture CLI if Quartz permissions fail.
    """
    try:
        if bounds and len(bounds) == 4 and bounds[2] > 5 and bounds[3] > 5:
            x, y, w, h = [int(v) for v in bounds]
            rect = Quartz.CGRectMake(x, y, w, h)
        else:
            rect = Quartz.CGRectInfinite

        cg_image = Quartz.CGWindowListCreateImage(
            rect,
            Quartz.kCGWindowListOptionOnScreenOnly,
            Quartz.kCGNullWindowID,
            Quartz.kCGWindowImageBestResolution | Quartz.kCGWindowImageBoundsIgnoreFraming
        )

        if cg_image is not None:
            width = Quartz.CGImageGetWidth(cg_image)
            height = Quartz.CGImageGetHeight(cg_image)
            if width > 0 and height > 0:
                color_space = Quartz.CGColorSpaceCreateDeviceRGB()
                bytes_per_row = 4 * width
                raw_data = bytearray(height * bytes_per_row)

                context = Quartz.CGBitmapContextCreate(
                    raw_data,
                    width,
                    height,
                    8,
                    bytes_per_row,
                    color_space,
                    Quartz.kCGImageAlphaPremultipliedLast | Quartz.kCGBitmapByteOrder32Big
                )
                Quartz.CGContextDrawImage(context, Quartz.CGRectMake(0, 0, width, height), cg_image)
                pil_img = Image.frombytes("RGBA", (width, height), bytes(raw_data)).convert("RGB")
                return pil_img

    except Exception as e:
        print(f"Quartz capture exception: {e}", file=sys.stderr)

    # Secondary Fallback: screencapture CLI
    temp_path = os.path.join(tempfile.gettempdir(), f"ai_snap_{os.getpid()}_{int(time.time()*1000)}.png")
    try:
        screencapture_bin = shutil.which("screencapture") or "/usr/sbin/screencapture"
        if bounds and len(bounds) == 4:
            x, y, w, h = [int(v) for v in bounds]
            subprocess.run([screencapture_bin, "-x", f"-R{x},{y},{w},{h}", temp_path], check=True)
        else:
            subprocess.run([screencapture_bin, "-x", temp_path], check=True)

        if os.path.exists(temp_path) and os.path.getsize(temp_path) > 0:
            with Image.open(temp_path) as img:
                pil_img = img.copy().convert("RGB")
            os.remove(temp_path)
            return pil_img
    except Exception as e:
        print(f"Screencapture fallback exception: {e}", file=sys.stderr)
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass

    return None


# ==============================================================================
# String, Markdown & Hotkey Utilities
# ==============================================================================

def clean_markdown(text):
    if not text:
        return ""
    text = re.sub(r"\*\*\*(.*?)\*\*\*", r"\1", text)
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = re.sub(r"\*(.*?)\*", r"\1", text)
    text = re.sub(r"___(.*?)___", r"\1", text)
    text = re.sub(r"__(.*?)__", r"\1", text)
    text = re.sub(r"_(.*?)_", r"\1", text)
    text = re.sub(r"(?m)^#{1,6}\s+", "", text)
    text = re.sub(r"(?m)^>\s+", "", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"```[a-zA-Z]*\n?", "", text)
    text = re.sub(r"```", "", text)
    text = re.sub(r"(?m)^[-*_]{3,}\s*$", "", text)
    return text.strip()


def normalize_hotkey(value):
    if value is None:
        return ""
    value = str(value).strip().lower()
    if not value:
        return ""
    tokens = re.split(r"[\s\-+]+", value)
    tokens = [t for t in tokens if t]
    if not tokens:
        return ""

    modifiers = []
    key = ""
    mapping = {
        "cmd": "cmd", "command": "cmd",
        "ctrl": "ctrl", "control": "ctrl",
        "opt": "opt", "option": "opt", "alt": "opt",
        "shift": "shift",
    }

    for token in tokens:
        if token in mapping:
            modifiers.append(mapping[token])
        elif not key:
            key = token
    if not key:
        return ""

    cleaned = [mod for mod in MODIFIER_ORDER if mod in modifiers]
    cleaned.append(key)
    return "-".join(cleaned)


def hotkey_event_to_string(event):
    char = (event.charactersIgnoringModifiers() or event.characters() or "").lower()
    if not char:
        return ""
    flags = event.modifierFlags()
    pieces = []
    if flags & (1 << 20):
        pieces.append("cmd")
    if flags & (1 << 18):
        pieces.append("ctrl")
    if flags & (1 << 19):
        pieces.append("opt")
    if flags & (1 << 17):
        pieces.append("shift")
    if pieces:
        pieces.append(char)
        return normalize_hotkey("-".join(pieces))
    return normalize_hotkey(char)


def clean_log_text(text, show_emojis):
    if not show_emojis:
        text = EMOJI_REGEX.sub("", text)
        text = re.sub(r"  +", " ", text).strip()
    return text


# ==============================================================================
# Robust JSON Configuration Persistence
# ==============================================================================

def load_config():
    defaults = {
        "api_key": "",
        "model_name": "gemini-3.7-flash",
        "text_color": [0.35, 0.95, 0.55, 1.0],   # Phosphor Green
        "bg_color": [0.06, 0.07, 0.09],          # Stealth Neutral
        "overlay_opacity": 0.85,                 # Master Window Opacity
        "window_bg_opacity": 0.85,               # Background Fill Opacity
        "text_opacity": 1.0,                     # Text Opacity
        "timer_opacity": 0.85,                   # Countdown Timer Opacity
        "show_dividers": True,                   # Enable/Disable --- dividers
        "show_emojis": True,                     # Enable/Disable emojis
        "log_ai_only": False,                    # Silent Mode (AI Output Only)
        "log_actions": True,                     # Action/Buffer capture logs
        "log_hotkeys": True,                     # Hotkey/Mode trigger logs
        "prompts": [
            "solve this for me",
            "summarize and provide a step-by-step concise answer",
            "extract code and solution only without fluff"
        ],
        "active_prompt_index": 0,
        "auto_interval": 5.0,
        "selected_crop_area": None,
        "master_key": "ctrl-opt-shift-s",
        "normal_key": "s",
        "scroll_key": "c",
        "toggle_window_key": "h",
        "clear_key": "x",
        "edit_region_key": "r",
        "auto_mode_key": "a",
    }
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    defaults.update(data)
                    print(f"✅ [Config] Loaded {len(data)} settings from: {CONFIG_FILE}")
        except Exception as e:
            print(f"❌ [Config] Error reading {CONFIG_FILE}: {e}", file=sys.stderr)
    else:
        print(f"ℹ️ [Config] No existing config found. Will save to: {CONFIG_FILE}")
    return defaults


def save_config(config):
    try:
        clean = {}
        for k, v in config.items():
            if isinstance(v, (str, int, float, bool)) or v is None:
                clean[k] = v
            elif isinstance(v, (list, tuple)):
                clean_list = []
                for item in v:
                    if isinstance(item, (int, float, bool, str)) or item is None:
                        clean_list.append(item)
                    else:
                        clean_list.append(str(item))
                clean[k] = clean_list
            else:
                clean[k] = str(v)

        json_str = json.dumps(clean, indent=2, ensure_ascii=False)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            f.write(json_str)
            f.flush()
            os.fsync(f.fileno())

        print(f"✅ [Config] Successfully saved {len(clean)} items to: {CONFIG_FILE}")
        return True
    except Exception as e:
        print(f"❌ [Config] Save failed for {CONFIG_FILE}: {e}", file=sys.stderr)
        return False


# ==============================================================================
# HUD & UI Overlay Panels
# ==============================================================================

class OverlayContentView(NSView):
    def hitTest_(self, point):
        hit = objc.super(OverlayContentView, self).hitTest_(point)
        if hit is not None:
            return hit
        if AppKit.NSPointInRect(point, self.bounds()):
            return self
        return None


class RegionEditOverlay(NSWindow):
    def initWithCallback_initialBounds_(self, callback, bounds):
        self.callback = callback
        screen_frame = NSScreen.mainScreen().frame()

        if bounds and len(bounds) == 4 and bounds[2] > 5 and bounds[3] > 5:
            bx, by, bw, bh = bounds
            cocoa_y = screen_frame.size.height - (by + bh)
            frame = NSMakeRect(bx, cocoa_y, bw, bh)
        else:
            w, h = 400, 250
            frame = NSMakeRect((screen_frame.size.width - w) / 2, (screen_frame.size.height - h) / 2, w, h)

        style = NSWindowStyleMaskNonactivatingPanel | NSWindowStyleMaskResizable
        self.initWithContentRect_styleMask_backing_defer_(frame, style, 2, False)
        self.setLevel_(NSStatusWindowLevel + 2)
        self.setOpaque_(False)
        self.setBackgroundColor_(NSColor.clearColor())
        self.setHasShadow_(False)
        self.setIgnoresMouseEvents_(False)
        self.setMovable_(True)
        self.setMovableByWindowBackground_(True)
        self.setAcceptsMouseMovedEvents_(True)
        self.setHidesOnDeactivate_(False)
        self.setSharingType_(NS_WINDOW_SHARING_NONE)

        self.overlay_view = NSTextField.alloc().initWithFrame_(self.contentView().bounds())
        self.overlay_view.setEditable_(False)
        self.overlay_view.setBordered_(False)
        self.overlay_view.setBackgroundColor_(NSColor.blackColor().colorWithAlphaComponent_(0.25))
        self.overlay_view.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        self.contentView().addSubview_(self.overlay_view)

        self.done_btn = NSButton.alloc().initWithFrame_(NSMakeRect(10, 10, 80, 24))
        self.done_btn.setTitle_("Lock Area")
        self.done_btn.setBezelStyle_(1)
        self.done_btn.setTarget_(self)
        self.done_btn.setAction_("finishEdit:")
        self.contentView().addSubview_(self.done_btn)
        return self

    def mouseDownCanMoveWindow_(self, event):
        return True

    def finishEdit_(self, sender):
        frame = self.frame()
        screen_frame = NSScreen.mainScreen().frame()
        left = frame.origin.x
        top = screen_frame.size.height - (frame.origin.y + frame.size.height)
        width = frame.size.width
        height = frame.size.height
        self.orderOut_(None)
        if width > 5 and height > 5:
            self.callback((int(left), int(top), int(width), int(height)))
        else:
            self.callback(None)


class CountdownOverlay(NSWindow):
    def init(self):
        screen = NSScreen.mainScreen()
        screen_frame = screen.visibleFrame() if screen is not None else NSScreen.mainScreen().frame()
        w, h = 140, 32
        frame = NSMakeRect(screen_frame.size.width - w - 20, screen_frame.size.height - h - 40, w, h)
        self.initWithContentRect_styleMask_backing_defer_(
            frame, NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel, 2, False
        )
        self.setLevel_(NSStatusWindowLevel + 3)
        self.setOpaque_(False)
        self.setBackgroundColor_(NSColor.blackColor().colorWithAlphaComponent_(0.70))
        self.setHasShadow_(False)
        self.setCanHide_(False)
        self.setHidesOnDeactivate_(False)
        self.setIgnoresMouseEvents_(True)
        self.setSharingType_(NS_WINDOW_SHARING_NONE)
        self.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces | NSWindowCollectionBehaviorFullScreenAuxiliary
        )

        self.label = NSTextField.labelWithString_("Auto: Off")
        self.label.setFrame_(NSMakeRect(8, 4, w - 16, h - 8))
        self.label.setTextColor_(NSColor.colorWithRed_green_blue_alpha_(0.35, 0.95, 0.55, 1.0))
        self.label.setFont_(NSFont.monospacedSystemFontOfSize_weight_(12, 0.3))
        self.contentView().addSubview_(self.label)
        return self

    @objc.python_method
    def update_status(self, text):
        app = NSApp().delegate()
        show_emojis = app.config.get("show_emojis", True) if app else True
        self.label.setStringValue_(clean_log_text(text, show_emojis))

    def setOverlayOpacity_(self, alpha):
        self.setAlphaValue_(alpha)


class HotkeyField(NSTextField):
    def initWithFrame_(self, frame):
        self = objc.super(HotkeyField, self).initWithFrame_(frame)
        if self is None:
            return None
        self._capturing = False
        self._pending_hotkey = ""
        self._last_value = ""
        self.setEditable_(False)
        self.setSelectable_(False)
        return self

    def acceptsFirstResponder(self):
        return True

    def mouseDown_(self, event):
        self._last_value = self.stringValue()
        if self.window() is not None:
            self.window().makeFirstResponder_(self)
        self._capturing = True
        self._pending_hotkey = ""
        self.setStringValue_("Press combo...")
        if hasattr(self.window(), "setEditingHotkeyField_"):
            self.window().setEditingHotkeyField_(self)

    def keyDown_(self, event):
        if not self._capturing:
            return
        value = hotkey_event_to_string(event)
        if value:
            self._pending_hotkey = value
            self.setStringValue_(value)

    def cancelOperation_(self, sender):
        if not self._capturing:
            return
        self._capturing = False
        self._pending_hotkey = ""
        self.setStringValue_(self._last_value)
        if self.window() is not None and hasattr(self.window(), "setEditingHotkeyField_"):
            self.window().setEditingHotkeyField_(None)

    def keyUp_(self, event):
        if not self._capturing:
            return
        value = self._pending_hotkey or hotkey_event_to_string(event)
        if value:
            self.setStringValue_(value)
            if self.window() is not None:
                self.window().makeFirstResponder_(None)
                if hasattr(self.window(), "setEditingHotkeyField_"):
                    self.window().setEditingHotkeyField_(None)
        else:
            self.setStringValue_(self._last_value)
        self._capturing = False
        self._pending_hotkey = ""


class AIOverlayPanel(NSWindow):
    def init(self):
        frame = NSMakeRect(60, 60, 580, 440)
        self.initWithContentRect_styleMask_backing_defer_(
            frame,
            NSWindowStyleMaskBorderless | NSWindowStyleMaskResizable,
            2, False
        )
        self.setTitle_("")
        self.setLevel_(NSStatusWindowLevel)
        self.setHasShadow_(False)
        self.setOpaque_(False)
        self.setHidesOnDeactivate_(False)
        self.setCanHide_(False)
        self.setReleasedWhenClosed_(False)
        self.setMovable_(True)
        self.setMovableByWindowBackground_(True)
        self.setShowsResizeIndicator_(True)
        self.setAcceptsMouseMovedEvents_(True)
        self.setIgnoresMouseEvents_(False)
        self.setSharingType_(NS_WINDOW_SHARING_NONE)
        self.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces | NSWindowCollectionBehaviorFullScreenAuxiliary
        )

        custom_content = OverlayContentView.alloc().initWithFrame_(self.contentView().bounds())
        custom_content.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        self.setContentView_(custom_content)

        content_bounds = self.contentView().bounds()

        # Full Buffer Scroll View (Takes entire window space)
        scroll_frame = NSMakeRect(8, 8, content_bounds.size.width - 16, content_bounds.size.height - 16)
        self.scroll_view = NSScrollView.alloc().initWithFrame_(scroll_frame)
        self.scroll_view.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        self.scroll_view.setDrawsBackground_(False)
        self.scroll_view.setHasVerticalScroller_(True)
        self.scroll_view.setHasHorizontalScroller_(False)
        self.scroll_view.setAutohidesScrollers_(True)
        self.scroll_view.setScrollerStyle_(1)  # NSScrollerStyleOverlay
        self.scroll_view.setScrollerKnobStyle_(2)  # NSScrollerKnobStyleDark

        content_size = self.scroll_view.contentSize()
        self.text_view = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, content_size.width, content_size.height))
        self.text_view.setMinSize_(NSMakeSize(0.0, content_size.height))
        self.text_view.setMaxSize_(NSMakeSize(10000000.0, 10000000.0))
        self.text_view.setVerticallyResizable_(True)
        self.text_view.setHorizontallyResizable_(False)
        self.text_view.setAutoresizingMask_(NSViewWidthSizable)
        self.text_view.textContainer().setContainerSize_(NSMakeSize(content_size.width, 10000000.0))
        self.text_view.textContainer().setWidthTracksTextView_(True)
        self.text_view.setDrawsBackground_(False)
        self.text_view.setEditable_(False)
        self.text_view.setSelectable_(True)
        self.text_view.setFont_(NSFont.monospacedSystemFontOfSize_weight_(12.0, 0.0))

        self.scroll_view.setDocumentView_(self.text_view)
        self.contentView().addSubview_(self.scroll_view)
        return self

    def canBecomeKeyWindow(self):
        return True

    def canBecomeMainWindow(self):
        return True

    def mouseDownCanMoveWindow_(self, event):
        return True

    @objc.python_method
    def apply_background(self):
        app = NSApp().delegate()
        if not app:
            return
        bg_rgb = app.config.get("bg_color", [0.06, 0.07, 0.09])
        bg_alpha = app.config.get("window_bg_opacity", 0.85)
        effective_alpha = max(0.002, bg_alpha) if bg_alpha == 0.0 else bg_alpha
        color = NSColor.colorWithRed_green_blue_alpha_(bg_rgb[0], bg_rgb[1], bg_rgb[2], effective_alpha)
        self.setBackgroundColor_(color)

    def setOverlayOpacity_(self, alpha):
        self.setAlphaValue_(alpha)

    def clearText_(self, sender):
        self.text_view.setString_("")
        app = NSApp().delegate()
        if app and hasattr(app, "print_initial_status"):
            app.print_initial_status()

    def appendText_(self, new_text):
        app = NSApp().delegate()
        show_dividers = app.config.get("show_dividers", True) if app else True
        show_emojis = app.config.get("show_emojis", True) if app else True

        cleaned_text = clean_markdown(str(new_text))
        cleaned_text = clean_log_text(cleaned_text, show_emojis)

        existing = self.text_view.string() or ""
        timestamp = time.strftime("[%H:%M:%S]")
        entry = f"{timestamp} {cleaned_text}"

        if existing:
            separator = "\n" + "-"*50 + "\n" if show_dividers else "\n"
        else:
            separator = ""

        full_text = existing + separator + entry
        self.text_view.setString_(full_text)

        if app and hasattr(app, "apply_stored_text_color"):
            app.apply_stored_text_color()

        length = len(full_text)
        self.text_view.scrollRangeToVisible_((length, 0))


# ==============================================================================
# Settings Window GUI
# ==============================================================================

class SettingsWindow(NSWindow):
    def initWithApp_(self, app_delegate):
        self.app_delegate = app_delegate

        style = NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
        frame = NSMakeRect(160, 40, 680, 830)
        self.initWithContentRect_styleMask_backing_defer_(frame, style, 2, False)

        self.setTitle_("Configuration, Model & Shortcuts")
        self.setHasShadow_(True)
        self.setOpaque_(True)
        self.setReleasedWhenClosed_(False)
        self.setMovable_(True)
        self.setMovableByWindowBackground_(True)
        self.setBackgroundColor_(NSColor.windowBackgroundColor())
        self.setHidesOnDeactivate_(False)
        self.setDelegate_(self)
        self.editing_hotkey_field = None

        NSApp().activateIgnoringOtherApps_(True)
        self.makeKeyAndOrderFront_(None)

        cfg = self.app_delegate.config

        # 1. API & Model Setup
        lbl_api = NSTextField.labelWithString_("Gemini API Key:")
        lbl_api.setFrame_(NSMakeRect(20, 785, 170, 20))
        self.contentView().addSubview_(lbl_api)

        self.api_field = NSTextField.alloc().initWithFrame_(NSMakeRect(200, 783, 440, 22))
        self.api_field.setStringValue_(str(cfg.get("api_key", "")))
        self.api_field.setEditable_(True)
        self.api_field.setSelectable_(True)
        self.api_field.setBordered_(True)
        self.api_field.setBezeled_(True)
        self.api_field.setUsesSingleLineMode_(True)
        self.contentView().addSubview_(self.api_field)

        lbl_model = NSTextField.labelWithString_("Gemini Model:")
        lbl_model.setFrame_(NSMakeRect(20, 750, 170, 20))
        self.contentView().addSubview_(lbl_model)

        self.model_field = NSTextField.alloc().initWithFrame_(NSMakeRect(200, 748, 440, 22))
        self.model_field.setStringValue_(str(cfg.get("model_name", "gemini-3.7-flash")))
        self.model_field.setEditable_(True)
        self.model_field.setSelectable_(True)
        self.model_field.setBordered_(True)
        self.model_field.setBezeled_(True)
        self.model_field.setUsesSingleLineMode_(True)
        self.contentView().addSubview_(self.model_field)

        # 2. Prompts 1, 2, 3
        prompts = cfg.get("prompts", ["solve this for me", "", ""])

        lbl_p1 = NSTextField.labelWithString_("Prompt 1 [Key 1]:")
        lbl_p1.setFrame_(NSMakeRect(20, 710, 170, 20))
        self.contentView().addSubview_(lbl_p1)

        self.p1_field = NSTextField.alloc().initWithFrame_(NSMakeRect(200, 708, 345, 22))
        self.p1_field.setStringValue_(str(prompts[0]) if len(prompts) > 0 else "")
        self.contentView().addSubview_(self.p1_field)

        self.p1_btn = NSButton.alloc().initWithFrame_(NSMakeRect(555, 706, 85, 24))
        self.p1_btn.setTitle_("Set Active")
        self.p1_btn.setBezelStyle_(1)
        self.p1_btn.setTarget_(self)
        self.p1_btn.setAction_("setP1Active:")
        self.contentView().addSubview_(self.p1_btn)

        lbl_p2 = NSTextField.labelWithString_("Prompt 2 [Key 2]:")
        lbl_p2.setFrame_(NSMakeRect(20, 675, 170, 20))
        self.contentView().addSubview_(lbl_p2)

        self.p2_field = NSTextField.alloc().initWithFrame_(NSMakeRect(200, 673, 345, 22))
        self.p2_field.setStringValue_(str(prompts[1]) if len(prompts) > 1 else "")
        self.contentView().addSubview_(self.p2_field)

        self.p2_btn = NSButton.alloc().initWithFrame_(NSMakeRect(555, 671, 85, 24))
        self.p2_btn.setTitle_("Set Active")
        self.p2_btn.setBezelStyle_(1)
        self.p2_btn.setTarget_(self)
        self.p2_btn.setAction_("setP2Active:")
        self.contentView().addSubview_(self.p2_btn)

        lbl_p3 = NSTextField.labelWithString_("Prompt 3 [Key 3]:")
        lbl_p3.setFrame_(NSMakeRect(20, 640, 170, 20))
        self.contentView().addSubview_(lbl_p3)

        self.p3_field = NSTextField.alloc().initWithFrame_(NSMakeRect(200, 638, 345, 22))
        self.p3_field.setStringValue_(str(prompts[2]) if len(prompts) > 2 else "")
        self.contentView().addSubview_(self.p3_field)

        self.p3_btn = NSButton.alloc().initWithFrame_(NSMakeRect(555, 636, 85, 24))
        self.p3_btn.setTitle_("Set Active")
        self.p3_btn.setBezelStyle_(1)
        self.p3_btn.setTarget_(self)
        self.p3_btn.setAction_("setP3Active:")
        self.contentView().addSubview_(self.p3_btn)

        self._update_prompt_btn_styles()

        # 3. Labeled Shortcuts
        lbl_master = NSTextField.labelWithString_("Master Mode Shortcut:")
        lbl_master.setFrame_(NSMakeRect(20, 595, 170, 20))
        self.contentView().addSubview_(lbl_master)

        self.master_field = HotkeyField.alloc().initWithFrame_(NSMakeRect(200, 593, 140, 22))
        self.master_field.setStringValue_(str(cfg.get("master_key", "ctrl-opt-shift-s")))
        self.contentView().addSubview_(self.master_field)

        lbl_stat = NSTextField.labelWithString_("Stationary Snap Key:")
        lbl_stat.setFrame_(NSMakeRect(360, 595, 150, 20))
        self.contentView().addSubview_(lbl_stat)

        self.normal_field = HotkeyField.alloc().initWithFrame_(NSMakeRect(515, 593, 125, 22))
        self.normal_field.setStringValue_(str(cfg.get("normal_key", "s")))
        self.contentView().addSubview_(self.normal_field)

        lbl_scroll = NSTextField.labelWithString_("Scrolling Snap Key:")
        lbl_scroll.setFrame_(NSMakeRect(20, 560, 170, 20))
        self.contentView().addSubview_(lbl_scroll)

        self.scroll_field = HotkeyField.alloc().initWithFrame_(NSMakeRect(200, 558, 140, 22))
        self.scroll_field.setStringValue_(str(cfg.get("scroll_key", "c")))
        self.contentView().addSubview_(self.scroll_field)

        lbl_tog = NSTextField.labelWithString_("Toggle HUD Window:")
        lbl_tog.setFrame_(NSMakeRect(360, 560, 150, 20))
        self.contentView().addSubview_(lbl_tog)

        self.toggle_window_field = HotkeyField.alloc().initWithFrame_(NSMakeRect(515, 558, 125, 22))
        self.toggle_window_field.setStringValue_(str(cfg.get("toggle_window_key", "h")))
        self.contentView().addSubview_(self.toggle_window_field)

        lbl_clear = NSTextField.labelWithString_("Clear Buffer Key:")
        lbl_clear.setFrame_(NSMakeRect(20, 525, 170, 20))
        self.contentView().addSubview_(lbl_clear)

        self.clear_field = HotkeyField.alloc().initWithFrame_(NSMakeRect(200, 523, 140, 22))
        self.clear_field.setStringValue_(str(cfg.get("clear_key", "x")))
        self.contentView().addSubview_(self.clear_field)

        lbl_edit = NSTextField.labelWithString_("Edit Crop Region Key:")
        lbl_edit.setFrame_(NSMakeRect(360, 525, 150, 20))
        self.contentView().addSubview_(lbl_edit)

        self.edit_region_field = HotkeyField.alloc().initWithFrame_(NSMakeRect(515, 523, 125, 22))
        self.edit_region_field.setStringValue_(str(cfg.get("edit_region_key", "r")))
        self.contentView().addSubview_(self.edit_region_field)

        lbl_auto = NSTextField.labelWithString_("Auto Mode Loop Key:")
        lbl_auto.setFrame_(NSMakeRect(20, 490, 170, 20))
        self.contentView().addSubview_(lbl_auto)

        self.auto_mode_field = HotkeyField.alloc().initWithFrame_(NSMakeRect(200, 488, 140, 22))
        self.auto_mode_field.setStringValue_(str(cfg.get("auto_mode_key", "a")))
        self.contentView().addSubview_(self.auto_mode_field)

        # 4. Status Logging Toggles
        self.dividers_checkbox = NSButton.alloc().initWithFrame_(NSMakeRect(200, 450, 200, 22))
        self.dividers_checkbox.setButtonType_(AppKit.NSButtonTypeSwitch)
        self.dividers_checkbox.setTitle_("Show Line Dividers (---)")
        self.dividers_checkbox.setState_(1 if cfg.get("show_dividers", True) else 0)
        self.contentView().addSubview_(self.dividers_checkbox)

        self.emojis_checkbox = NSButton.alloc().initWithFrame_(NSMakeRect(420, 450, 180, 22))
        self.emojis_checkbox.setButtonType_(AppKit.NSButtonTypeSwitch)
        self.emojis_checkbox.setTitle_("Show Status Emojis")
        self.emojis_checkbox.setState_(1 if cfg.get("show_emojis", True) else 0)
        self.contentView().addSubview_(self.emojis_checkbox)

        self.ai_only_checkbox = NSButton.alloc().initWithFrame_(NSMakeRect(200, 420, 210, 22))
        self.ai_only_checkbox.setButtonType_(AppKit.NSButtonTypeSwitch)
        self.ai_only_checkbox.setTitle_("Silent Mode (AI Output Only)")
        self.ai_only_checkbox.setState_(1 if cfg.get("log_ai_only", False) else 0)
        self.contentView().addSubview_(self.ai_only_checkbox)

        self.action_logs_checkbox = NSButton.alloc().initWithFrame_(NSMakeRect(420, 420, 200, 22))
        self.action_logs_checkbox.setButtonType_(AppKit.NSButtonTypeSwitch)
        self.action_logs_checkbox.setTitle_("Show Action Capture Logs")
        self.action_logs_checkbox.setState_(1 if cfg.get("log_actions", True) else 0)
        self.contentView().addSubview_(self.action_logs_checkbox)

        # 5. Opacity Controls (0.0 to 1.0)
        lbl_win_op = NSTextField.labelWithString_("Master Window Opacity:")
        lbl_win_op.setFrame_(NSMakeRect(20, 375, 170, 20))
        self.contentView().addSubview_(lbl_win_op)

        self.master_opacity_slider = NSSlider.alloc().initWithFrame_(NSMakeRect(200, 373, 440, 24))
        self.master_opacity_slider.setMinValue_(0.0)
        self.master_opacity_slider.setMaxValue_(1.0)
        self.master_opacity_slider.setDoubleValue_(float(cfg.get("overlay_opacity", 0.85)))
        self.master_opacity_slider.setTarget_(self)
        self.master_opacity_slider.setAction_("masterOpacityChanged:")
        self.contentView().addSubview_(self.master_opacity_slider)

        lbl_bg_op = NSTextField.labelWithString_("Background Opacity:")
        lbl_bg_op.setFrame_(NSMakeRect(20, 335, 170, 20))
        self.contentView().addSubview_(lbl_bg_op)

        self.bg_opacity_slider = NSSlider.alloc().initWithFrame_(NSMakeRect(200, 333, 440, 24))
        self.bg_opacity_slider.setMinValue_(0.0)
        self.bg_opacity_slider.setMaxValue_(1.0)
        self.bg_opacity_slider.setDoubleValue_(float(cfg.get("window_bg_opacity", 0.85)))
        self.bg_opacity_slider.setTarget_(self)
        self.bg_opacity_slider.setAction_("bgOpacityChanged:")
        self.contentView().addSubview_(self.bg_opacity_slider)

        lbl_text_op = NSTextField.labelWithString_("Text Opacity:")
        lbl_text_op.setFrame_(NSMakeRect(20, 295, 170, 20))
        self.contentView().addSubview_(lbl_text_op)

        self.text_opacity_slider = NSSlider.alloc().initWithFrame_(NSMakeRect(200, 293, 440, 24))
        self.text_opacity_slider.setMinValue_(0.0)
        self.text_opacity_slider.setMaxValue_(1.0)
        self.text_opacity_slider.setDoubleValue_(float(cfg.get("text_opacity", 1.0)))
        self.text_opacity_slider.setTarget_(self)
        self.text_opacity_slider.setAction_("textOpacityChanged:")
        self.contentView().addSubview_(self.text_opacity_slider)

        lbl_timer_op = NSTextField.labelWithString_("Auto Timer Opacity:")
        lbl_timer_op.setFrame_(NSMakeRect(20, 255, 170, 20))
        self.contentView().addSubview_(lbl_timer_op)

        self.timer_opacity_slider = NSSlider.alloc().initWithFrame_(NSMakeRect(200, 253, 440, 24))
        self.timer_opacity_slider.setMinValue_(0.0)
        self.timer_opacity_slider.setMaxValue_(1.0)
        self.timer_opacity_slider.setDoubleValue_(float(cfg.get("timer_opacity", 0.85)))
        self.timer_opacity_slider.setTarget_(self)
        self.timer_opacity_slider.setAction_("timerOpacityChanged:")
        self.contentView().addSubview_(self.timer_opacity_slider)

        # 6. Colors
        lbl_color = NSTextField.labelWithString_("Text Colour:")
        lbl_color.setFrame_(NSMakeRect(20, 200, 170, 20))
        self.contentView().addSubview_(lbl_color)

        t_rgba = cfg.get("text_color", [0.35, 0.95, 0.55, 1.0])
        self.text_color_well = NSColorWell.alloc().initWithFrame_(NSMakeRect(200, 196, 100, 28))
        self.text_color_well.setColor_(NSColor.colorWithRed_green_blue_alpha_(t_rgba[0], t_rgba[1], t_rgba[2], t_rgba[3]))
        self.text_color_well.setTarget_(self)
        self.text_color_well.setAction_("textColorChanged:")
        self.contentView().addSubview_(self.text_color_well)

        self.text_color_preview = NSView.alloc().initWithFrame_(NSMakeRect(310, 198, 24, 24))
        self.text_color_preview.setWantsLayer_(True)
        self.text_color_preview.layer().setBackgroundColor_(self.text_color_well.color().CGColor())
        self.contentView().addSubview_(self.text_color_preview)

        lbl_bg_color = NSTextField.labelWithString_("Background Colour:")
        lbl_bg_color.setFrame_(NSMakeRect(20, 155, 170, 20))
        self.contentView().addSubview_(lbl_bg_color)

        bg_stored = cfg.get("bg_color", [0.06, 0.07, 0.09])
        self.bg_color_well = NSColorWell.alloc().initWithFrame_(NSMakeRect(200, 151, 100, 28))
        self.bg_color_well.setColor_(NSColor.colorWithRed_green_blue_alpha_(bg_stored[0], bg_stored[1], bg_stored[2], 1.0))
        self.bg_color_well.setTarget_(self)
        self.bg_color_well.setAction_("bgColorChanged:")
        self.contentView().addSubview_(self.bg_color_well)

        self.bg_color_preview = NSView.alloc().initWithFrame_(NSMakeRect(310, 153, 24, 24))
        self.bg_color_preview.setWantsLayer_(True)
        self.bg_color_preview.layer().setBackgroundColor_(self.bg_color_well.color().CGColor())
        self.contentView().addSubview_(self.bg_color_preview)

        # 7. Save & Apply
        self.save_btn = NSButton.alloc().initWithFrame_(NSMakeRect(540, 20, 100, 28))
        self.save_btn.setTitle_("Save & Apply")
        self.save_btn.setBezelStyle_(1)
        self.save_btn.setTarget_(self)
        self.save_btn.setAction_("saveSettings:")
        self.contentView().addSubview_(self.save_btn)

        return self

    def _update_prompt_btn_styles(self):
        idx = getattr(self.app_delegate, "active_prompt_index", 0)
        self.p1_btn.setTitle_("✓ Active" if idx == 0 else "Set Active")
        self.p2_btn.setTitle_("✓ Active" if idx == 1 else "Set Active")
        self.p3_btn.setTitle_("✓ Active" if idx == 2 else "Set Active")

    def setP1Active_(self, sender):
        self.app_delegate.active_prompt_index = 0
        self.app_delegate.config["active_prompt_index"] = 0
        self._update_prompt_btn_styles()
        save_config(self.app_delegate.config)
        self.app_delegate.log_to_window("🎯 [Prompt 1 Selected]", tag="hotkey")

    def setP2Active_(self, sender):
        self.app_delegate.active_prompt_index = 1
        self.app_delegate.config["active_prompt_index"] = 1
        self._update_prompt_btn_styles()
        save_config(self.app_delegate.config)
        self.app_delegate.log_to_window("🎯 [Prompt 2 Selected]", tag="hotkey")

    def setP3Active_(self, sender):
        self.app_delegate.active_prompt_index = 2
        self.app_delegate.config["active_prompt_index"] = 2
        self._update_prompt_btn_styles()
        save_config(self.app_delegate.config)
        self.app_delegate.log_to_window("🎯 [Prompt 3 Selected]", tag="hotkey")

    def setEditingHotkeyField_(self, field):
        self.editing_hotkey_field = field

    def windowShouldClose_(self, sender):
        self.orderOut_(None)
        self.editing_hotkey_field = None
        self.app_delegate.settings_win = None
        return True

    def masterOpacityChanged_(self, sender):
        alpha = float(sender.doubleValue())
        self.app_delegate.config["overlay_opacity"] = alpha
        if hasattr(self.app_delegate, "panel"):
            self.app_delegate.panel.setOverlayOpacity_(alpha)

    def bgOpacityChanged_(self, sender):
        alpha = float(sender.doubleValue())
        self.app_delegate.config["window_bg_opacity"] = alpha
        if hasattr(self.app_delegate, "panel"):
            self.app_delegate.panel.apply_background()

    def textOpacityChanged_(self, sender):
        alpha = float(sender.doubleValue())
        self.app_delegate.config["text_opacity"] = alpha
        self.app_delegate.apply_stored_text_color()

    def timerOpacityChanged_(self, sender):
        alpha = float(sender.doubleValue())
        self.app_delegate.config["timer_opacity"] = alpha
        if hasattr(self.app_delegate, "countdown_overlay"):
            self.app_delegate.countdown_overlay.setOverlayOpacity_(alpha)

    def textColorChanged_(self, sender):
        color = sender.color()
        rgb_color = color.colorUsingColorSpace_(NSColorSpace.sRGBColorSpace())
        if rgb_color is not None:
            self.app_delegate.config["text_color"] = [
                float(rgb_color.redComponent()),
                float(rgb_color.greenComponent()),
                float(rgb_color.blueComponent()),
                float(rgb_color.alphaComponent()),
            ]
            self.text_color_preview.layer().setBackgroundColor_(rgb_color.CGColor())
            self.app_delegate.apply_stored_text_color()

    def bgColorChanged_(self, sender):
        color = sender.color()
        rgb_color = color.colorUsingColorSpace_(NSColorSpace.sRGBColorSpace())
        if rgb_color is not None:
            self.app_delegate.config["bg_color"] = [
                float(rgb_color.redComponent()),
                float(rgb_color.greenComponent()),
                float(rgb_color.blueComponent()),
            ]
            self.bg_color_preview.layer().setBackgroundColor_(rgb_color.CGColor())
            if hasattr(self.app_delegate, "panel"):
                self.app_delegate.panel.apply_background()

    def saveSettings_(self, sender):
        try:
            cfg = dict(self.app_delegate.config)
            cfg["api_key"] = str(self.api_field.stringValue() or "").strip()
            cfg["model_name"] = str(self.model_field.stringValue() or "").strip() or "gemini-3.7-flash"
            cfg["prompts"] = [
                str(self.p1_field.stringValue() or ""),
                str(self.p2_field.stringValue() or ""),
                str(self.p3_field.stringValue() or "")
            ]
            cfg["active_prompt_index"] = int(self.app_delegate.active_prompt_index)
            cfg["master_key"] = normalize_hotkey(str(self.master_field.stringValue() or "")) or "ctrl-opt-shift-s"
            cfg["normal_key"] = normalize_hotkey(str(self.normal_field.stringValue() or "")) or "s"
            cfg["scroll_key"] = normalize_hotkey(str(self.scroll_field.stringValue() or "")) or "c"
            cfg["toggle_window_key"] = normalize_hotkey(str(self.toggle_window_field.stringValue() or "")) or "h"
            cfg["clear_key"] = normalize_hotkey(str(self.clear_field.stringValue() or "")) or "x"
            cfg["edit_region_key"] = normalize_hotkey(str(self.edit_region_field.stringValue() or "")) or "r"
            cfg["auto_mode_key"] = normalize_hotkey(str(self.auto_mode_field.stringValue() or "")) or "a"
            cfg["show_dividers"] = bool(self.dividers_checkbox.state() == 1)
            cfg["show_emojis"] = bool(self.emojis_checkbox.state() == 1)
            cfg["log_ai_only"] = bool(self.ai_only_checkbox.state() == 1)
            cfg["log_actions"] = bool(self.action_logs_checkbox.state() == 1)
            cfg["overlay_opacity"] = float(self.master_opacity_slider.doubleValue())
            cfg["window_bg_opacity"] = float(self.bg_opacity_slider.doubleValue())
            cfg["text_opacity"] = float(self.text_opacity_slider.doubleValue())
            cfg["timer_opacity"] = float(self.timer_opacity_slider.doubleValue())

            # Read color wells
            try:
                t_col = self.text_color_well.color().colorUsingColorSpace_(NSColorSpace.sRGBColorSpace())
                if t_col is not None:
                    cfg["text_color"] = [float(t_col.redComponent()), float(t_col.greenComponent()), float(t_col.blueComponent()), float(t_col.alphaComponent())]
            except Exception:
                pass

            try:
                b_col = self.bg_color_well.color().colorUsingColorSpace_(NSColorSpace.sRGBColorSpace())
                if b_col is not None:
                    cfg["bg_color"] = [float(b_col.redComponent()), float(b_col.greenComponent()), float(b_col.blueComponent())]
            except Exception:
                pass

            # Save to disk
            ok = save_config(cfg)
            if ok:
                self.app_delegate.config = cfg
                self.app_delegate.reinit_client()
                self.app_delegate.apply_stored_text_color()

                if hasattr(self.app_delegate, "panel"):
                    self.app_delegate.panel.apply_background()
                    self.app_delegate.panel.setOverlayOpacity_(cfg["overlay_opacity"])

                if hasattr(self.app_delegate, "countdown_overlay"):
                    self.app_delegate.countdown_overlay.setOverlayOpacity_(cfg["timer_opacity"])

                self.app_delegate.log_to_window(f"💾 [Settings] Saved to {os.path.basename(CONFIG_FILE)}", tag="system")
            else:
                self.app_delegate.log_to_window("❌ [Settings] Failed to save config to disk.", tag="system")

        except Exception as e:
            print(f"[Settings] Error in saveSettings: {e}", file=sys.stderr)
            import traceback
            traceback.print_exc()

        finally:
            self.orderOut_(None)
            self.app_delegate.settings_win = None


# ==============================================================================
# Application Controller (skhd Event Tap Engine)
# ==============================================================================

class AppDelegate:
    def applicationDidFinishLaunching_(self, notification):
        self.config = load_config()
        self._normalize_config_hotkeys()
        self.selected_crop_area = self._load_selected_crop_area()
        self.active_prompt_index = int(self.config.get("active_prompt_index", 0))
        self.region_overlay = None
        self.settings_win = None
        self.is_scrolling_mode = False
        self.auto_mode_active = False
        self.master_activation_active = False
        self.client = None

        self.reinit_client()

        app = NSApp()
        app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
        app.activateIgnoringOtherApps_(False)

        self.panel = AIOverlayPanel.alloc().init()
        self.panel.apply_background()
        self.panel.setOverlayOpacity_(self.config.get("overlay_opacity", 0.85))
        self.panel.makeKeyAndOrderFront_(None)
        self.panel.orderFrontRegardless()
        self.apply_stored_text_color()

        self._install_edit_menu()

        self.countdown_overlay = CountdownOverlay.alloc().init()
        self.countdown_overlay.setOverlayOpacity_(self.config.get("timer_opacity", 0.85))

        self.status_item = NSStatusBar.systemStatusBar().statusItemWithLength_(-1)
        self.status_item.button().setTitle_("🤖")

        self.menu = NSMenu.alloc().init()

        self.sel_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Edit Screenshot Area...", "editRegion:", ""
        )
        self.sel_item.setTarget_(self)
        self.menu.addItem_(self.sel_item)

        self.mode_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Capture Type: Stationary", "toggleCaptureMode:", ""
        )
        self.mode_item.setTarget_(self)
        self.menu.addItem_(self.mode_item)

        self.toggle_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Toggle Overlay Window", "toggleWindowVisibility:", ""
        )
        self.toggle_item.setTarget_(self)
        self.menu.addItem_(self.toggle_item)

        self.menu.addItem_(NSMenuItem.separatorItem())

        self.auto_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Start Auto Loop (5s)", "toggleAutoInterval:", ""
        )
        self.auto_item.setTarget_(self)
        self.menu.addItem_(self.auto_item)

        self.master_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Activate Master Mode", "activateMasterFromMenu:", ""
        )
        self.master_item.setTarget_(self)
        self.menu.addItem_(self.master_item)

        self.menu.addItem_(NSMenuItem.separatorItem())

        self.settings_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Settings GUI...", "openSettings:", ""
        )
        self.settings_item.setTarget_(self)
        self.menu.addItem_(self.settings_item)

        self.quit_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Quit App", "terminate:", ""
        )
        self.menu.addItem_(self.quit_item)
        self.status_item.setMenu_(self.menu)

        # Initialize Low-Level Event Tap
        self.event_tap = None
        self.event_tap_source = None
        self._init_skhd_event_tap()

        # Check Screen Recording Permission on startup
        check_and_request_screen_recording()

        self.print_initial_status()

    @objc.python_method
    def print_initial_status(self):
        active_model = self.config.get("model_name", "gemini-3.7-flash")
        self.log_to_window(f"🚀 [System] AI HUD Daemon ready. Engine: {active_model}", tag="system")
        if self.selected_crop_area:
            x, y, w, h = self.selected_crop_area
            self.log_to_window(f"🎯 [Target] Active Crop: {w}x{h} at ({x}, {y})", tag="system")
        else:
            self.log_to_window("🎯 [Target] Fullscreen Mode active.", tag="system")

    def _init_skhd_event_tap(self):
        trusted = check_and_request_accessibility(prompt=True)
        if not trusted:
            self.log_to_window("⚠️ [Accessibility] Permission required. Grant access in System Settings > Privacy & Security > Accessibility.", tag="system")

        mask = Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown)
        self.event_tap = Quartz.CGEventTapCreate(
            Quartz.kCGHIDEventTap,
            Quartz.kCGHeadInsertEventTap,
            Quartz.kCGEventTapOptionDefault,
            mask,
            self._skhd_event_tap_callback,
            None,
        )

        if self.event_tap is None:
            self.event_tap = Quartz.CGEventTapCreate(
                Quartz.kCGSessionEventTap,
                Quartz.kCGHeadInsertEventTap,
                Quartz.kCGEventTapOptionDefault,
                mask,
                self._skhd_event_tap_callback,
                None,
            )

        if self.event_tap is not None:
            self.event_tap_source = Quartz.CFMachPortCreateRunLoopSource(None, self.event_tap, 0)
            Quartz.CFRunLoopAddSource(Quartz.CFRunLoopGetMain(), self.event_tap_source, Quartz.kCFRunLoopCommonModes)
            Quartz.CGEventTapEnable(self.event_tap, True)
            self.log_to_window("⚡ [skhd Tap] Low-level Quartz event stream hooked.", tag="system")
        else:
            self.log_to_window("❌ [Error] Failed to create Event Tap. Check macOS accessibility permissions.", tag="system")

    def _skhd_event_tap_callback(self, proxy, event_type, event, refcon):
        if event_type in (Quartz.kCGEventTapDisabledByTimeout, Quartz.kCGEventTapDisabledByUserInput):
            if self.event_tap is not None:
                Quartz.CGEventTapEnable(self.event_tap, True)
            return event

        if event_type != Quartz.kCGEventKeyDown:
            return event

        settings = getattr(self, "settings_win", None)
        if settings is not None and getattr(settings, "editing_hotkey_field", None) is not None:
            return event

        ns_event = NSEvent.eventWithCGEvent_(event)
        if ns_event is None:
            return event

        hotkey = hotkey_event_to_string(ns_event)
        if not hotkey:
            return event

        swallowed = self._evaluate_and_dispatch_hotkey(hotkey)
        if swallowed:
            return None

        return event

    def _evaluate_and_dispatch_hotkey(self, current_hotkey):
        master_key = normalize_hotkey(self.config.get("master_key", "ctrl-opt-shift-s"))
        normal_key = normalize_hotkey(self.config.get("normal_key", "s"))
        scroll_key = normalize_hotkey(self.config.get("scroll_key", "c"))
        toggle_window_key = normalize_hotkey(self.config.get("toggle_window_key", "h"))
        clear_key = normalize_hotkey(self.config.get("clear_key", "x"))
        edit_region_key = normalize_hotkey(self.config.get("edit_region_key", "r"))
        auto_mode_key = normalize_hotkey(self.config.get("auto_mode_key", "a"))

        def key_tail(hk):
            return hk.split("-")[-1] if hk else ""

        current_tail = key_tail(current_hotkey)
        normal_tail = key_tail(normal_key)
        scroll_tail = key_tail(scroll_key)
        toggle_window_tail = key_tail(toggle_window_key)
        clear_tail = key_tail(clear_key)
        edit_tail = key_tail(edit_region_key)
        auto_tail = key_tail(auto_mode_key)

        # 1. Master Key Toggle (Locks or Unlocks Master Mode)
        if current_hotkey == master_key:
            self.master_activation_active = not self.master_activation_active
            self._refresh_menu_labels()
            if self.master_activation_active:
                self.log_to_window(
                    "🔮 [Master Mode: LOCKED ON]\n"
                    "   Keys: [S] Snap | [C] Scroll | [H] HUD | [X] Clear | [R] Crop | [A] Auto\n"
                    "   Prompts: [1] Prompt 1 | [2] Prompt 2 | [3] Prompt 3\n"
                    "   (Press Master shortcut again to unlock)",
                    tag="hotkey"
                )
            else:
                self.log_to_window("🔓 [Master Mode: UNLOCKED] Standard typing restored.", tag="hotkey")
            return True

        # 2. While Master Mode is Active
        if self.master_activation_active:
            prompts = self.config.get("prompts", ["solve this for me", "", ""])

            # Switch Prompts with 1, 2, 3
            if current_hotkey == "1" or current_tail == "1":
                self.active_prompt_index = 0
                self.config["active_prompt_index"] = 0
                save_config(self.config)
                p_text = prompts[0] if len(prompts) > 0 else ""
                self.log_to_window(f"🎯 [Prompt 1 Activated]: \"{p_text}\"", tag="hotkey")
                return True
            elif current_hotkey == "2" or current_tail == "2":
                self.active_prompt_index = 1
                self.config["active_prompt_index"] = 1
                save_config(self.config)
                p_text = prompts[1] if len(prompts) > 1 else ""
                self.log_to_window(f"🎯 [Prompt 2 Activated]: \"{p_text}\"", tag="hotkey")
                return True
            elif current_hotkey == "3" or current_tail == "3":
                self.active_prompt_index = 2
                self.config["active_prompt_index"] = 2
                save_config(self.config)
                p_text = prompts[2] if len(prompts) > 2 else ""
                self.log_to_window(f"🎯 [Prompt 3 Activated]: \"{p_text}\"", tag="hotkey")
                return True

            # Action Triggers
            if current_hotkey == normal_key or current_tail == normal_tail:
                self.log_to_window("⌨️ [Hotkey: S] Stationary Capture triggered.", tag="hotkey")
                AppHelper.callAfter(self.execute_pipeline, False)
                return True
            elif current_hotkey == scroll_key or current_tail == scroll_tail:
                self.log_to_window("⌨️ [Hotkey: C] Scrolling Capture triggered.", tag="hotkey")
                AppHelper.callAfter(self.execute_pipeline, True)
                return True
            elif current_hotkey == toggle_window_key or current_tail == toggle_window_tail:
                self.log_to_window("⌨️ [Hotkey: Toggle] Overlay Visibility toggled.", tag="hotkey")
                AppHelper.callAfter(self.toggleWindowVisibility_, None)
                return True
            elif current_hotkey == clear_key or current_tail == clear_tail:
                AppHelper.callAfter(self.panel.clearText_, None)
                return True
            elif current_hotkey == edit_region_key or current_tail == edit_tail:
                self.log_to_window("📐 [Hotkey: R] Opening region selector overlay...", tag="hotkey")
                AppHelper.callAfter(self.editRegion_, None)
                return True
            elif current_hotkey == auto_mode_key or current_tail == auto_tail:
                AppHelper.callAfter(self.toggleAutoInterval_, None)
                return True
            return False

        # 3. Direct Key Combos (outside Master Mode)
        if current_hotkey == normal_key and self._can_trigger_outside_master(normal_key):
            self.log_to_window("⌨️ [Combo] Stationary Capture triggered.", tag="hotkey")
            AppHelper.callAfter(self.execute_pipeline, False)
            return True
        elif current_hotkey == scroll_key and self._can_trigger_outside_master(scroll_key):
            self.log_to_window("⌨️ [Combo] Scrolling Capture triggered.", tag="hotkey")
            AppHelper.callAfter(self.execute_pipeline, True)
            return True
        elif current_hotkey == toggle_window_key and self._can_trigger_outside_master(toggle_window_key):
            AppHelper.callAfter(self.toggleWindowVisibility_, None)
            return True
        elif current_hotkey == clear_key and self._can_trigger_outside_master(clear_key):
            AppHelper.callAfter(self.panel.clearText_, None)
            return True
        elif current_hotkey == edit_region_key and self._can_trigger_outside_master(edit_region_key):
            AppHelper.callAfter(self.editRegion_, None)
            return True
        elif current_hotkey == auto_mode_key and self._can_trigger_outside_master(auto_mode_key):
            AppHelper.callAfter(self.toggleAutoInterval_, None)
            return True

        return False

    def _can_trigger_outside_master(self, configured_key):
        return bool(configured_key) and "-" in configured_key

    def _normalize_config_hotkeys(self):
        defaults = {
            "master_key": "ctrl-opt-shift-s",
            "normal_key": "s",
            "scroll_key": "c",
            "toggle_window_key": "h",
            "clear_key": "x",
            "edit_region_key": "r",
            "auto_mode_key": "a",
        }
        for key, fallback in defaults.items():
            normalized = normalize_hotkey(self.config.get(key, ""))
            self.config[key] = normalized or fallback

    def _load_selected_crop_area(self):
        area = self.config.get("selected_crop_area")
        if not isinstance(area, (list, tuple)) or len(area) != 4:
            return None
        try:
            x, y, w, h = [int(v) for v in area]
            return (x, y, w, h) if w > 5 and h > 5 else None
        except Exception:
            return None

    def _save_selected_crop_area(self):
        self.config["selected_crop_area"] = list(self.selected_crop_area) if self.selected_crop_area else None
        save_config(self.config)

    def _refresh_menu_labels(self):
        self.master_item.setTitle_(
            "Deactivate Master Mode" if self.master_activation_active else "Activate Master Mode"
        )
        self.auto_item.setTitle_(
            "Stop Auto Loop" if self.auto_mode_active else "Start Auto Loop (5s)"
        )

    def _install_edit_menu(self):
        main_menu = NSMenu.alloc().initWithTitle_("MainMenu")
        app_menu_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("App", "", "")
        app_menu = NSMenu.alloc().initWithTitle_("App")
        app_menu.addItem_(NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Quit", "terminate:", "q"))
        app_menu_item.setSubmenu_(app_menu)
        main_menu.addItem_(app_menu_item)

        edit_menu_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Edit", "", "")
        edit_menu = NSMenu.alloc().initWithTitle_("Edit")
        for title, action, key in [
            ("Cut", "cut:", "x"), ("Copy", "copy:", "c"), ("Paste", "paste:", "v"),
            ("Select All", "selectAll:", "a")
        ]:
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, key)
            item.setTarget_(None)
            edit_menu.addItem_(item)
        edit_menu_item.setSubmenu_(edit_menu)
        main_menu.addItem_(edit_menu_item)
        NSApp().setMainMenu_(main_menu)

    def reinit_client(self):
        try:
            api_key = str(self.config.get("api_key", "")).strip()
            if not api_key:
                api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or ""
            if api_key:
                self.client = genai.Client(api_key=api_key)
            else:
                self.client = None
        except Exception:
            self.client = None

    def apply_stored_text_color(self):
        c = self.config["text_color"]
        alpha = self.config.get("text_opacity", 1.0)
        color = NSColor.colorWithRed_green_blue_alpha_(c[0], c[1], c[2], alpha)
        font = NSFont.monospacedSystemFontOfSize_weight_(12.0, 0.0)

        self.panel.text_view.setTextColor_(color)
        self.panel.text_view.setFont_(font)

        length = len(self.panel.text_view.string() or "")
        if length > 0:
            self.panel.text_view.textStorage().addAttribute_value_range_("NSColor", color, (0, length))
            self.panel.text_view.textStorage().addAttribute_value_range_("NSFont", font, (0, length))

    def log_to_window(self, text, tag="general"):
        if self.config.get("log_ai_only", False) and tag != "ai" and not text.startswith("❌") and not text.startswith("⚠️"):
            return
        if tag == "action" and not self.config.get("log_actions", True):
            return
        if tag == "hotkey" and not self.config.get("log_hotkeys", True):
            return

        AppHelper.callAfter(self.panel.appendText_, str(text))

    def editRegion_(self, sender):
        if self.region_overlay is not None and self.region_overlay.isVisible():
            self.region_overlay.makeKeyAndOrderFront_(None)
            NSApp().activateIgnoringOtherApps_(True)
            return

        self.region_overlay = RegionEditOverlay.alloc().initWithCallback_initialBounds_(
            self.region_selected_callback_, self.selected_crop_area
        )
        self.region_overlay.makeKeyAndOrderFront_(None)
        NSApp().activateIgnoringOtherApps_(True)

    def region_selected_callback_(self, bounds):
        if bounds:
            self.selected_crop_area = bounds
            self._save_selected_crop_area()
            self.log_to_window(f"🎯 [Target] Locked region: {bounds[2]}x{bounds[3]} at ({bounds[0]}, {bounds[1]})", tag="system")
        else:
            self.log_to_window("⚠️ [Target] Region selection aborted.", tag="system")

    def toggleCaptureMode_(self, sender):
        self.is_scrolling_mode = not self.is_scrolling_mode
        mode_str = "Scrolling" if self.is_scrolling_mode else "Stationary"
        self.mode_item.setTitle_(f"Capture Type: {mode_str}")
        self.log_to_window(f"🔄 [Mode] Capture mode changed to {mode_str}.", tag="system")

    def toggleWindowVisibility_(self, sender):
        if self.panel.isVisible():
            self.panel.orderOut_(None)
            self.toggle_item.setTitle_("Show Overlay Window")
        else:
            self.panel.makeKeyAndOrderFront_(None)
            self.panel.orderFrontRegardless()
            self.toggle_item.setTitle_("Hide Overlay Window")

    def openSettings_(self, sender):
        if getattr(self, "settings_win", None) is not None:
            self.settings_win.orderOut_(None)
            self.settings_win = None
        self.settings_win = SettingsWindow.alloc().initWithApp_(self)

    def activateMasterFromMenu_(self, sender):
        self.master_activation_active = not self.master_activation_active
        self._refresh_menu_labels()
        if self.master_activation_active:
            self.log_to_window("🔮 [Master Mode: LOCKED ON] Keys: [S] Snap | [C] Scroll | [H] HUD | [X] Clear | [R] Crop | [A] Auto | [1/2/3] Prompts", tag="hotkey")
        else:
            self.log_to_window("🔓 [Master Mode: UNLOCKED] Standard typing restored.", tag="hotkey")

    def toggleAutoInterval_(self, sender):
        if self.auto_mode_active:
            self.auto_mode_active = False
            self._refresh_menu_labels()
            self.countdown_overlay.orderOut_(None)
            self.log_to_window("🛑 [Auto Mode] Loop paused.", tag="action")
        else:
            if not self.selected_crop_area:
                self.log_to_window("❌ [Auto Mode] Select a target area first before starting auto loop.", tag="action")
                return

            self.auto_mode_active = True
            self._refresh_menu_labels()
            self.countdown_overlay.update_status("Auto: 5s")
            self.countdown_overlay.makeKeyAndOrderFront_(None)
            self.countdown_overlay.orderFrontRegardless()
            self.log_to_window("⏱️ [Auto Mode] 5-second recurring loop started.", tag="action")
            self.run_countdown_tick(5)

    def run_countdown_tick(self, val):
        if not self.auto_mode_active:
            return

        self.countdown_overlay.orderFrontRegardless()

        if val <= 0:
            self.countdown_overlay.update_status("📸 Capture!")
            self.execute_pipeline()
            threading.Timer(1.0, lambda: AppHelper.callAfter(self.run_countdown_tick, 5)).start()
        else:
            self.countdown_overlay.update_status(f"Auto: {val}s")
            threading.Timer(1.0, lambda: AppHelper.callAfter(self.run_countdown_tick, val - 1)).start()

    def execute_pipeline(self, override_scroll=None):
        threading.Thread(target=self._async_task, args=(override_scroll,), daemon=True).start()

    def _async_task(self, override_scroll):
        scrolling = self.is_scrolling_mode if override_scroll is None else override_scroll
        mode_str = "Scrolling" if scrolling else "Stationary"
        model_name = self.config.get("model_name", "gemini-3.7-flash").strip() or "gemini-3.7-flash"
        start_time = time.time()

        prompts = self.config.get("prompts", ["solve this for me", "", ""])
        idx = getattr(self, "active_prompt_index", 0)
        if idx >= len(prompts) or idx < 0:
            idx = 0
        base_prompt = prompts[idx] or "solve this for me"

        # 1. Target region log
        if self.selected_crop_area:
            x, y, w, h = self.selected_crop_area
            self.log_to_window(f"📸 [Capture] Region: {w}x{h} at ({x}, {y}) ({mode_str} Mode) [P{idx+1}]...", tag="action")
        else:
            self.log_to_window(f"📸 [Capture] Fullscreen ({mode_str} Mode) [P{idx+1}]...", tag="action")

        if not self.client:
            self.log_to_window("❌ [Error] API Client uninitialized. Enter your API Key in Settings GUI.", tag="ai")
            return

        # 2. In-memory capture
        shot = capture_screen_to_pil(self.selected_crop_area)

        if shot is None:
            self.log_to_window(
                "⚠️ [Capture Error]: Screen Recording permission missing. "
                "Enable it in System Settings > Privacy & Security > Screen Recording for Terminal/Python.",
                tag="ai"
            )
            return

        # 3. Buffer telemetry log
        buf = io.BytesIO()
        shot.save(buf, format="PNG")
        file_size_kb = len(buf.getvalue()) / 1024.0
        width, height = shot.size
        self.log_to_window(f"🖼️ [Buffer] Captured image ({width}x{height}, {file_size_kb:.1f} KB). Sending to {model_name}...", tag="action")

        # 4. Dispatch to Gemini
        try:
            prompt = base_prompt
            if scrolling:
                prompt += (
                    " This is a continuous scrolling frame tracking sequence capture. "
                    "Solve the appended cascading context details."
                )

            res = self.client.models.generate_content(
                model=model_name,
                contents=[prompt, shot]
            )

            elapsed = time.time() - start_time
            ai_output = res.text or "(Empty Response)"
            self.log_to_window(f"🤖 [AI Engine ({model_name})] Solved in {elapsed:.2f}s:\n\n{ai_output}", tag="ai")

        except Exception as e:
            self.log_to_window(f"⚠️ [Pipeline Error]: {str(e)}", tag="ai")


if __name__ == "__main__":
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    app.activateIgnoringOtherApps_(False)
    delegate = AppDelegate()
    app.setDelegate_(delegate)
    sys.exit(app.run())
