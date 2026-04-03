"""
Input controller for Windows XP Solitaire.

Translates solver moves into mouse inputs sent to the sol.exe window.

Uses real cursor movement (SetCursorPos) + SendInput for reliable input.
XP Solitaire requires actual cursor positioning for drag operations.

Mouse actions:
  - Double-click: auto-move card to foundation (if possible)
  - Click and drag: move card(s) from source to destination
  - Single click on stock: draw card(s)
"""

import ctypes
import ctypes.wintypes as wt
import time
import sys
from typing import Optional, Tuple

from game_state import Card, Pile, PileType, GameState
from solver import Move, MoveType

# Windows API
user32 = ctypes.windll.user32

# Window messages (for PostMessage fallback)
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_MOUSEMOVE = 0x0200
WM_COMMAND = 0x0111

# Mouse event flags for SendInput
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_ABSOLUTE = 0x8000

# Virtual key codes
INPUT_MOUSE = 0

# sol.exe menu command IDs
MENU_NEW_GAME = 0x12D  # 301 — Game > Deal Again

# Card visual dimensions (approximate client-area pixels)
CARD_WIDTH = 71
CARD_HEIGHT = 96

# Verbose logging (set by caller)
_verbose = False


def set_verbose(v: bool):
    global _verbose
    _verbose = v


def _vlog(msg: str):
    if _verbose:
        print(f"  [input] {msg}", file=sys.stderr)


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", wt.DWORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wt.WORD),
        ("wScan", wt.WORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wt.DWORD),
        ("wParamL", wt.WORD),
        ("wParamH", wt.WORD),
    ]


class INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
        ("hi", HARDWAREINPUT),
    ]


class INPUT(ctypes.Structure):
    _fields_ = [
        ("type", wt.DWORD),
        ("union", INPUT_UNION),
    ]


def make_lparam(x: int, y: int) -> int:
    """Pack x,y into an LPARAM for PostMessage."""
    return ((y & 0xFFFF) << 16) | (x & 0xFFFF)


class InputController:
    """
    Controls sol.exe by moving the real cursor and using SendInput.

    For reliable card moves in XP Solitaire, we:
    1. Bring the window to the foreground
    2. Convert card client-area coords to screen coords
    3. Use SetCursorPos + SendInput for click/drag operations
    """

    def __init__(self, move_delay: float = 0.2, fast: bool = False):
        self.hwnd: Optional[int] = None
        self.move_delay = move_delay
        self.fast = fast
        # Micro-delays between input steps
        self._step_delay = 0.01 if fast else 0.05
        self._tiny_delay = 0.005 if fast else 0.02
        self._drag_step_delay = 0.005 if fast else 0.02
        self._fg_delay = 0.01 if fast else 0.05
        self._find_window()

    def _find_window(self):
        """Find the Solitaire window handle."""
        self.hwnd = user32.FindWindowW("Solitaire", None)
        if not self.hwnd:
            raise RuntimeError("Could not find Solitaire window")

    def _client_to_screen(self, cx: int, cy: int) -> Tuple[int, int]:
        """Convert client-area coordinates to screen coordinates."""
        point = wt.POINT(cx, cy)
        user32.ClientToScreen(self.hwnd, ctypes.byref(point))
        return (point.x, point.y)

    def _ensure_foreground(self):
        """Bring Solitaire to the foreground."""
        user32.SetForegroundWindow(self.hwnd)
        time.sleep(self._fg_delay)

    def _move_cursor(self, screen_x: int, screen_y: int):
        """Move the cursor to screen coordinates."""
        user32.SetCursorPos(screen_x, screen_y)
        time.sleep(self._tiny_delay)

    def _send_mouse_down(self):
        """Send a left mouse button down event via SendInput."""
        inp = INPUT()
        inp.type = INPUT_MOUSE
        inp.union.mi.dwFlags = MOUSEEVENTF_LEFTDOWN
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        time.sleep(self._tiny_delay)

    def _send_mouse_up(self):
        """Send a left mouse button up event via SendInput."""
        inp = INPUT()
        inp.type = INPUT_MOUSE
        inp.union.mi.dwFlags = MOUSEEVENTF_LEFTUP
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        time.sleep(self._tiny_delay)

    def _click_at(self, client_x: int, client_y: int):
        """Click at client-area coordinates using real cursor movement."""
        self._ensure_foreground()
        sx, sy = self._client_to_screen(client_x, client_y)
        _vlog(f"click at client({client_x},{client_y}) -> screen({sx},{sy})")
        self._move_cursor(sx, sy)
        time.sleep(self._step_delay)
        self._send_mouse_down()
        time.sleep(self._step_delay)
        self._send_mouse_up()
        time.sleep(self.move_delay)

    def _double_click_at(self, client_x: int, client_y: int):
        """Double-click at client-area coordinates."""
        self._ensure_foreground()
        sx, sy = self._client_to_screen(client_x, client_y)
        _vlog(f"double-click at client({client_x},{client_y}) -> screen({sx},{sy})")
        self._move_cursor(sx, sy)
        time.sleep(self._step_delay)
        self._send_mouse_down()
        time.sleep(self._tiny_delay)
        self._send_mouse_up()
        time.sleep(self._step_delay)
        self._send_mouse_down()
        time.sleep(self._tiny_delay)
        self._send_mouse_up()
        time.sleep(self.move_delay)

    def _drag(self, from_cx: int, from_cy: int, to_cx: int, to_cy: int):
        """Drag from one client-area position to another."""
        self._ensure_foreground()
        from_sx, from_sy = self._client_to_screen(from_cx, from_cy)
        to_sx, to_sy = self._client_to_screen(to_cx, to_cy)
        _vlog(f"drag from client({from_cx},{from_cy})->({to_cx},{to_cy})  "
              f"screen({from_sx},{from_sy})->({to_sx},{to_sy})")

        # Move to source
        self._move_cursor(from_sx, from_sy)
        time.sleep(self._step_delay)

        # Mouse down at source
        self._send_mouse_down()
        time.sleep(self._step_delay)

        # Move to destination (interpolate for smoother drag)
        steps = 3 if self.fast else 5
        for i in range(1, steps + 1):
            t = i / steps
            ix = int(from_sx + (to_sx - from_sx) * t)
            iy = int(from_sy + (to_sy - from_sy) * t)
            self._move_cursor(ix, iy)
            time.sleep(self._drag_step_delay)

        time.sleep(self._step_delay)

        # Mouse up at destination
        self._send_mouse_up()
        time.sleep(self.move_delay)

    # ---- Position helpers ----

    def _card_click_pos(self, pile: Pile, card_index: int = -1) -> Tuple[int, int]:
        """
        Get click position (client coords) for a card in a pile.
        Card x,y from memory are top-left of the card in client area.

        For cards that have another card overlapping below them (i.e. not
        the last card in the pile), we click close to the top of the card
        (y + 5) because the clickable region is only the small exposed strip
        before the next card starts.  For the bottom-most (last) card in a
        pile we can click further down.
        """
        if pile.is_empty:
            return (pile.x + CARD_WIDTH // 2, pile.y + CARD_HEIGHT // 2)

        if card_index == -1:
            card_index = len(pile.cards) - 1

        card = pile.cards[card_index]
        is_last = (card_index == len(pile.cards) - 1)
        y_offset = CARD_HEIGHT // 4 if is_last else 5
        return (card.x + CARD_WIDTH // 2, card.y + y_offset)

    def _pile_base_pos(self, pile: Pile) -> Tuple[int, int]:
        """Click position for an empty pile's base area."""
        return (pile.x + CARD_WIDTH // 2, pile.y + CARD_HEIGHT // 2)

    def _dest_drop_pos(self, pile: Pile) -> Tuple[int, int]:
        """
        Get the drop target position for a destination pile.
        For non-empty piles, drop on the top card.
        For empty piles, drop on the pile base.
        """
        if pile.is_empty:
            return self._pile_base_pos(pile)
        else:
            return self._card_click_pos(pile, -1)

    # ---- Move execution ----

    def execute_move(self, move: Move, state: GameState):
        """Execute a move by sending mouse inputs to sol.exe."""
        if move.move_type == MoveType.DRAW_STOCK:
            self._do_draw_stock(state)
        elif move.move_type == MoveType.RECYCLE_WASTE:
            self._do_recycle_stock(state)
        elif move.move_type == MoveType.WASTE_TO_FOUNDATION:
            self._do_waste_to_foundation(state)
        elif move.move_type == MoveType.WASTE_TO_TABLEAU:
            self._do_waste_to_tableau(move, state)
        elif move.move_type == MoveType.TABLEAU_TO_FOUNDATION:
            self._do_tableau_to_foundation(move, state)
        elif move.move_type == MoveType.TABLEAU_TO_TABLEAU:
            self._do_tableau_to_tableau(move, state)

    def _do_draw_stock(self, state: GameState):
        """Click the stock pile to draw card(s)."""
        x, y = self._pile_base_pos(state.stock)
        self._click_at(x, y)

    def _do_recycle_stock(self, state: GameState):
        """Click the empty stock to recycle waste."""
        x, y = self._pile_base_pos(state.stock)
        self._click_at(x, y)

    def _do_waste_to_foundation(self, state: GameState):
        """Double-click waste top card to auto-move to foundation."""
        if state.waste.top_card:
            x, y = self._card_click_pos(state.waste)
            self._double_click_at(x, y)

    def _do_waste_to_tableau(self, move: Move, state: GameState):
        """Drag waste top card to destination tableau column."""
        if not state.waste.top_card:
            return

        src_x, src_y = self._card_click_pos(state.waste)
        dst_idx = move.dest - PileType.TABLEAU_0
        dst_pile = state.tableau[dst_idx]
        dst_x, dst_y = self._dest_drop_pos(dst_pile)
        self._drag(src_x, src_y, dst_x, dst_y)

    def _do_tableau_to_foundation(self, move: Move, state: GameState):
        """Double-click tableau top card to auto-move to foundation."""
        src_idx = move.source - PileType.TABLEAU_0
        src_pile = state.tableau[src_idx]
        if src_pile.top_card:
            x, y = self._card_click_pos(src_pile)
            self._double_click_at(x, y)

    def _do_tableau_to_tableau(self, move: Move, state: GameState):
        """Drag card(s) from source tableau to destination tableau."""
        src_idx = move.source - PileType.TABLEAU_0
        src_pile = state.tableau[src_idx]
        if not src_pile.cards:
            return

        # Click the start of the sequence being moved
        card_index = len(src_pile.cards) - move.num_cards
        src_x, src_y = self._card_click_pos(src_pile, card_index)

        dst_idx = move.dest - PileType.TABLEAU_0
        dst_pile = state.tableau[dst_idx]
        dst_x, dst_y = self._dest_drop_pos(dst_pile)

        self._drag(src_x, src_y, dst_x, dst_y)

    def flip_top_card(self, pile: Pile):
        """
        Click on the top card of a pile to flip it face-up.
        Call this when the pile has only face-down cards remaining.
        """
        if pile.is_empty:
            return
        top = pile.cards[-1]
        if not top.face_down:
            return  # Already face-up
        x, y = self._card_click_pos(pile, len(pile.cards) - 1)
        _vlog(f"flipping top face-down card at client({x},{y})")
        self._click_at(x, y)

    def accept_deal_again(self):
        """
        After a win, wait for the animation to finish, then start a new
        game: send F2 to trigger "Deal Again?", then Space to accept Yes.
        """
        time.sleep(5.0)
        # Send F2 to bring up the Deal Again dialog
        self._ensure_foreground()
        VK_F2 = 0x71
        user32.keybd_event(VK_F2, 0, 0, 0)
        time.sleep(0.05)
        user32.keybd_event(VK_F2, 0, 0x0002, 0)
        time.sleep(1.0)
        # Send Space to press the Yes button
        VK_SPACE = 0x20
        user32.keybd_event(VK_SPACE, 0, 0, 0)
        time.sleep(0.05)
        user32.keybd_event(VK_SPACE, 0, 0x0002, 0)
        time.sleep(1.0)

    def new_game(self):
        """Start a new game by sending F2 (Deal Again)."""
        self._ensure_foreground()
        # Send F2 keypress via keybd_event
        VK_F2 = 0x71
        user32.keybd_event(VK_F2, 0, 0, 0)          # key down
        time.sleep(0.05)
        user32.keybd_event(VK_F2, 0, 0x0002, 0)     # key up (KEYEVENTF_KEYUP)
        time.sleep(1.0)

    def is_window_alive(self) -> bool:
        """Check if the Solitaire window still exists."""
        return bool(user32.IsWindow(self.hwnd))
