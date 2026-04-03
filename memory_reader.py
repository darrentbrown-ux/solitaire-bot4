"""
Memory reader for Windows XP Solitaire (sol.exe).

Reads the game state directly from the process memory using Windows API calls
(OpenProcess, ReadProcessMemory). No OCR or image recognition needed.

Memory layout (reverse engineered from sol.exe PE32, ImageBase 0x01000000):

  Global pointer:
    0x01007170 -> game object (heap-allocated)

  Game object:
    +0x64: pile count (DWORD, always 13 for Klondike)
    +0x6c: pile[0]  pointer (stock)
    +0x70: pile[1]  pointer (waste)
    +0x74: pile[2]  pointer (foundation 0)
    +0x78: pile[3]  pointer (foundation 1)
    +0x7c: pile[4]  pointer (foundation 2)
    +0x80: pile[5]  pointer (foundation 3)
    +0x84: pile[6]  pointer (tableau 0)
    +0x88: pile[7]  pointer (tableau 1)
    +0x8c: pile[8]  pointer (tableau 2)
    +0x90: pile[9]  pointer (tableau 3)
    +0x94: pile[10] pointer (tableau 4)
    +0x98: pile[11] pointer (tableau 5)
    +0x9c: pile[12] pointer (tableau 6)

  Pile object:
    +0x00: vtable pointer
    +0x04: dispatch method pointer
    +0x08: x position (DWORD)
    +0x0c: y position (DWORD)
    +0x1c: card count (DWORD)
    +0x24: card array start (12 bytes per card)

  Card (12 bytes):
    +0x00: WORD - flags | card_id
      bit 15 (0x8000) = face-UP flag (set=face up, clear=face down)
      bits 0-5 = card identity (0-51)
        card_id % 4 = suit (0=Clubs, 1=Diamonds, 2=Hearts, 3=Spades)
        card_id / 4 = rank (0=Ace .. 12=King)
    +0x02: WORD - padding
    +0x04: DWORD - x coordinate
    +0x08: DWORD - y coordinate

  Other globals:
    0x0100702c: draw count (DWORD, 1 or 3)
    0x01007344: game number / seed
"""

import ctypes
import ctypes.wintypes as wt
import struct
import subprocess
import time
import os
import sys
from typing import Optional, List, Tuple

from game_state import (
    Card, Pile, PileType, GameState, Suit, Rank,
    FACE_UP_FLAG, CARD_ID_MASK,
)

# Windows API constants
PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400
TH32CS_SNAPPROCESS = 0x00000002

# sol.exe memory addresses (ImageBase = 0x01000000)
GAME_OBJECT_PTR_ADDR = 0x01007170
DRAW_COUNT_ADDR = 0x0100702C
GAME_NUMBER_ADDR = 0x01007344

# Game object offsets
PILE_COUNT_OFFSET = 0x64
PILE_ARRAY_OFFSET = 0x6C

# Pile object offsets
PILE_X_OFFSET = 0x08
PILE_Y_OFFSET = 0x0C
PILE_CARD_COUNT_OFFSET = 0x1C
PILE_CARD_ARRAY_OFFSET = 0x24

# Card structure
CARD_SIZE = 12  # bytes per card
CARD_WORD_OFFSET = 0x00
CARD_X_OFFSET = 0x04
CARD_Y_OFFSET = 0x08

# Expected pile count for Klondike
EXPECTED_PILE_COUNT = 13
MAX_CARDS_PER_PILE = 52  # safety limit

# Windows API
kernel32 = ctypes.windll.kernel32
psapi = ctypes.windll.psapi


class MemoryReadError(Exception):
    """Raised when memory reading fails."""
    pass


class ProcessNotFoundError(Exception):
    """Raised when sol.exe process is not found."""
    pass


class GameNotStartedError(Exception):
    """Raised when sol.exe is running but no game is active."""
    pass


def find_process_id(process_name: str = "sol.exe") -> Optional[int]:
    """Find the PID of a running process by name."""
    # Use CreateToolhelp32Snapshot to enumerate processes
    class PROCESSENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wt.DWORD),
            ("cntUsage", wt.DWORD),
            ("th32ProcessID", wt.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wt.DWORD),
            ("cntThreads", wt.DWORD),
            ("th32ParentProcessID", wt.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wt.DWORD),
            ("szExeFile", ctypes.c_char * 260),
        ]

    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == -1:
        return None

    try:
        entry = PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32)

        if not kernel32.Process32First(snapshot, ctypes.byref(entry)):
            return None

        while True:
            name = entry.szExeFile.decode("utf-8", errors="ignore").lower()
            if name == process_name.lower():
                return entry.th32ProcessID
            if not kernel32.Process32Next(snapshot, ctypes.byref(entry)):
                break
    finally:
        kernel32.CloseHandle(snapshot)

    return None


def launch_solitaire(exe_path: str = r"C:\Games\SOL_ENGLISH\sol.exe") -> int:
    """Launch sol.exe and return its PID."""
    if not os.path.exists(exe_path):
        raise FileNotFoundError(f"Solitaire not found at: {exe_path}")

    proc = subprocess.Popen([exe_path])
    time.sleep(2)  # Wait for the window to initialize

    pid = find_process_id("sol.exe")
    if pid is None:
        raise ProcessNotFoundError("Failed to launch sol.exe")
    return pid


class MemoryReader:
    """Reads game state from sol.exe process memory."""

    def __init__(self, pid: int):
        self.pid = pid
        self.process_handle = None
        self._open_process()

    def _open_process(self):
        """Open the process for reading."""
        self.process_handle = kernel32.OpenProcess(
            PROCESS_VM_READ | PROCESS_QUERY_INFORMATION,
            False,
            self.pid,
        )
        if not self.process_handle:
            error = ctypes.get_last_error()
            raise MemoryReadError(
                f"Cannot open process {self.pid} (error {error}). "
                "Try running as Administrator."
            )

    def close(self):
        """Close the process handle."""
        if self.process_handle:
            kernel32.CloseHandle(self.process_handle)
            self.process_handle = None

    def __del__(self):
        self.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _read_bytes(self, address: int, size: int) -> bytes:
        """Read raw bytes from process memory."""
        buffer = ctypes.create_string_buffer(size)
        bytes_read = ctypes.c_size_t(0)

        success = kernel32.ReadProcessMemory(
            self.process_handle,
            ctypes.c_void_p(address),
            buffer,
            size,
            ctypes.byref(bytes_read),
        )

        if not success or bytes_read.value != size:
            raise MemoryReadError(
                f"Failed to read {size} bytes at 0x{address:08X}"
            )

        return buffer.raw

    def _read_dword(self, address: int) -> int:
        """Read a 32-bit unsigned integer."""
        data = self._read_bytes(address, 4)
        return struct.unpack("<I", data)[0]

    def _read_word(self, address: int) -> int:
        """Read a 16-bit unsigned integer."""
        data = self._read_bytes(address, 2)
        return struct.unpack("<H", data)[0]

    def _read_signed_dword(self, address: int) -> int:
        """Read a 32-bit signed integer."""
        data = self._read_bytes(address, 4)
        return struct.unpack("<i", data)[0]

    def read_game_state(self) -> GameState:
        """Read the complete game state from sol.exe memory."""
        # Step 1: Read the game object pointer
        game_ptr = self._read_dword(GAME_OBJECT_PTR_ADDR)
        if game_ptr == 0:
            raise GameNotStartedError(
                "No active game (game object pointer is null). "
                "Start a new game in Solitaire first."
            )

        # Step 2: Verify pile count
        pile_count = self._read_dword(game_ptr + PILE_COUNT_OFFSET)
        if pile_count != EXPECTED_PILE_COUNT:
            raise GameNotStartedError(
                f"Unexpected pile count: {pile_count} (expected {EXPECTED_PILE_COUNT}). "
                "The game may not be initialized yet."
            )

        # Step 3: Read draw count
        draw_count = self._read_dword(DRAW_COUNT_ADDR)
        if draw_count not in (1, 3):
            draw_count = 1  # Default to draw-1

        # Step 4: Read all pile pointers
        pile_ptrs = []
        for i in range(EXPECTED_PILE_COUNT):
            ptr = self._read_dword(game_ptr + PILE_ARRAY_OFFSET + i * 4)
            pile_ptrs.append(ptr)

        # Step 5: Read each pile
        piles = []
        for i, ptr in enumerate(pile_ptrs):
            pile_type = PileType(i)
            pile = self._read_pile(ptr, pile_type)
            piles.append(pile)

        # Step 6: Assemble GameState
        state = GameState(
            stock=piles[0],
            waste=piles[1],
            foundations=piles[2:6],
            tableau=piles[6:13],
            draw_count=draw_count,
        )

        return state

    def _read_pile(self, pile_ptr: int, pile_type: PileType) -> Pile:
        """Read a single pile from memory."""
        if pile_ptr == 0:
            return Pile(pile_type=pile_type)

        # Read pile metadata
        pile_x = self._read_signed_dword(pile_ptr + PILE_X_OFFSET)
        pile_y = self._read_signed_dword(pile_ptr + PILE_Y_OFFSET)
        card_count = self._read_dword(pile_ptr + PILE_CARD_COUNT_OFFSET)

        # Safety check
        if card_count > MAX_CARDS_PER_PILE:
            card_count = 0

        # Read cards
        cards = []
        card_array_start = pile_ptr + PILE_CARD_ARRAY_OFFSET

        for j in range(card_count):
            card_addr = card_array_start + j * CARD_SIZE
            card = self._read_card(card_addr)
            cards.append(card)

        return Pile(
            pile_type=pile_type,
            cards=cards,
            x=pile_x,
            y=pile_y,
        )

    def _read_card(self, address: int) -> Card:
        """Read a single card (12 bytes) from memory."""
        data = self._read_bytes(address, CARD_SIZE)
        word, _, card_x, card_y = struct.unpack("<HHiI", data)
        return Card.from_memory(word, card_x, card_y)

    def read_game_number(self) -> int:
        """Read the current game number (seed)."""
        return self._read_dword(GAME_NUMBER_ADDR)

    def is_process_alive(self) -> bool:
        """Check if the sol.exe process is still running."""
        exit_code = wt.DWORD()
        result = kernel32.GetExitCodeProcess(
            self.process_handle, ctypes.byref(exit_code)
        )
        # STILL_ACTIVE = 259
        return result and exit_code.value == 259
