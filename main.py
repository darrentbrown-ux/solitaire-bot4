"""
Solitaire Bot 4 — Perfect-information Windows XP Solitaire player.

Reads ALL cards (including face-down) from sol.exe process memory,
solves the game completely before making any moves, then executes
the winning sequence. If the game is unsolvable, redeals and tries again.

Press Escape at any time to exit.
"""

import argparse
import sys
import time
import os

# Check platform early
if sys.platform != "win32":
    print("Error: This bot only runs on Windows (requires process memory access).")
    print("Install sol.exe at C:\\Games\\SOL_ENGLISH\\ and run on Windows.")
    sys.exit(1)

import keyboard  # Global hotkey support (works regardless of focus)

from memory_reader import (
    MemoryReader, find_process_id, launch_solitaire,
    ProcessNotFoundError, MemoryReadError, GameNotStartedError,
)
from game_state import GameState
from solver import Move, MoveType
from perfect_solver import PerfectSolverWrapper, SolveResult
from input_controller import InputController, set_verbose as set_input_verbose


DEFAULT_EXE_PATH = r"C:\Games\SOL_ENGLISH\sol.exe"
DEFAULT_MOVE_DELAY = 0.2
DEFAULT_SOLVE_TIMEOUT = 30.0
DEFAULT_MAX_STOCK_PASSES = 5
READ_RETRY_DELAY = 0.5
POST_MOVE_READ_DELAY = 0.3
FAST_MOVE_DELAY = 0.02
FAST_POST_MOVE_READ_DELAY = 0.05


class SolitaireBot:
    """Main bot controller — perfect-information solver + input automation."""

    def __init__(self, args):
        self.exe_path = args.exe
        self.move_delay = args.speed
        self.post_move_delay = POST_MOVE_READ_DELAY
        if args.fast:
            self.move_delay = FAST_MOVE_DELAY
            self.post_move_delay = FAST_POST_MOVE_READ_DELAY
        self.verbose = args.verbose
        self.no_launch = args.no_launch
        self._fast = args.fast
        self.solve_timeout = args.solve_timeout
        self.max_attempts = args.max_attempts
        self.max_stock_passes = args.max_stock_passes

        self.reader: MemoryReader = None
        self.controller: InputController = None
        self.perfect_solver: PerfectSolverWrapper = None
        self.running = True
        set_input_verbose(self.verbose)

        # Stats
        self.games_attempted = 0
        self.games_solved = 0
        self.games_won = 0
        self.games_unsolvable = 0
        self.total_moves = 0
        self.total_solve_time = 0.0
        self.total_nodes = 0

    def log(self, msg: str):
        """Print a message (always)."""
        print(f"[Bot] {msg}")

    def vlog(self, msg: str):
        """Print a verbose message."""
        if self.verbose:
            print(f"  > {msg}")

    def start(self):
        """Main entry point."""
        self.log("Solitaire Bot 4 \u2014 Perfect Information Solver")
        self.log(f"Solve timeout: {self.solve_timeout}s | "
                f"Max stock passes: {self.max_stock_passes} | "
                f"Max attempts: {self.max_attempts or 'unlimited'}")
        self.log("Press ESCAPE at any time to stop.")
        self.log("")

        # Register global escape handler
        keyboard.on_press_key("esc", lambda _: self._on_escape())

        try:
            self._ensure_solitaire_running()
            self._connect()
            self._play_loop()
        except KeyboardInterrupt:
            self.log("Interrupted.")
        except Exception as e:
            self.log(f"Fatal error: {e}")
            if self.verbose:
                import traceback
                traceback.print_exc()
        finally:
            self._cleanup()
            self._print_stats()

    def _on_escape(self):
        """Called when Escape is pressed (global hotkey)."""
        self.log("Escape pressed \u2014 stopping bot...")
        self.running = False

    def _ensure_solitaire_running(self):
        """Find or launch sol.exe."""
        pid = find_process_id("sol.exe")
        if pid:
            self.log(f"Found running sol.exe (PID: {pid})")
        elif self.no_launch:
            raise ProcessNotFoundError(
                "sol.exe is not running and --no-launch was specified."
            )
        else:
            self.log(f"Launching Solitaire from: {self.exe_path}")
            pid = launch_solitaire(self.exe_path)
            self.log(f"Launched sol.exe (PID: {pid})")

    def _connect(self):
        """Connect to the sol.exe process."""
        pid = find_process_id("sol.exe")
        if not pid:
            raise ProcessNotFoundError("sol.exe disappeared!")

        self.reader = MemoryReader(pid)
        self.controller = InputController(
            move_delay=self.move_delay, fast=self._fast
        )
        self.perfect_solver = PerfectSolverWrapper(
            timeout=self.solve_timeout,
            max_stock_passes=self.max_stock_passes,
            verbose=self.verbose,
        )

        self.log("Connected to Solitaire.")

    def _reconnect(self) -> bool:
        """Try to reconnect if the process was restarted."""
        try:
            if self.reader:
                self.reader.close()
            pid = find_process_id("sol.exe")
            if not pid:
                return False
            self.reader = MemoryReader(pid)
            self.controller = InputController(
                move_delay=self.move_delay, fast=self._fast
            )
            return True
        except Exception:
            return False

    def _play_loop(self):
        """Main game loop: solve \u2192 play \u2192 redeal \u2192 repeat."""
        while self.running:
            try:
                # Check attempt limit
                if self.max_attempts > 0 and self.games_attempted >= self.max_attempts:
                    self.log(f"Reached max attempts ({self.max_attempts}). Stopping.")
                    break

                result = self._solve_and_play()

                if not self.running:
                    break

                if result == "won":
                    self.log("Waiting for win animation...")
                    self.controller.accept_deal_again()
                else:
                    # Unsolvable or failed \u2014 redeal
                    self.log("Dealing new game...")
                    self.controller.new_game()

            except GameNotStartedError:
                self.vlog("No active game, waiting...")
                time.sleep(1.0)
            except MemoryReadError as e:
                self.vlog(f"Memory read error: {e}")
                if not self._reconnect():
                    self.log("Lost connection to sol.exe.")
                    break
                time.sleep(1.0)
            except Exception as e:
                self.log(f"Error during play: {e}")
                if self.verbose:
                    import traceback
                    traceback.print_exc()
                time.sleep(1.0)

    def _solve_and_play(self) -> str:
        """
        Read game state, solve, and play.
        Returns: "won", "unsolvable", or "failed"
        """
        self.games_attempted += 1
        self.log(f"--- Game #{self.games_attempted} ---")

        # Phase 1: Read the full game state (all cards visible in memory)
        state = self._read_state()
        if self.verbose:
            print(state.display())
            self._display_hidden_cards(state)

        # Phase 2: Solve
        self.log("Solving...")
        result = self.perfect_solver.solve(state)
        self.total_solve_time += result.elapsed
        self.total_nodes += result.nodes_explored

        if not result.solved:
            self.games_unsolvable += 1
            self.log(f"\u274c Unsolvable: {result.reason} "
                    f"({result.nodes_explored:,} nodes in {result.elapsed:.2f}s)")
            return "unsolvable"

        self.games_solved += 1
        self.log(f"\u2705 Solution found: {len(result.moves)} moves "
                f"({result.nodes_explored:,} nodes in {result.elapsed:.2f}s)")

        # Phase 3: Execute the pre-computed move sequence
        return self._execute_solution()

    def _execute_solution(self) -> str:
        """
        Execute the pre-computed winning move sequence.

        Re-reads game state from memory BEFORE each move to get accurate
        card screen coordinates. Verifies each move succeeded by checking
        that the game state actually changed. Retries failed moves up to
        3 times before aborting.

        Returns "won" or "failed".
        """
        move_count = 0
        max_retries = 3

        while self.running:
            move = self.perfect_solver.get_next_move()
            if move is None:
                # All moves executed \u2014 check if we actually won
                state = self._read_state()
                if state.is_won:
                    self.games_won += 1
                    self.log(f"\U0001f389 Game WON! ({move_count} moves executed)")
                    return "won"
                else:
                    self.log(f"\u26a0 Solution executed but game not won "
                            f"({move_count} moves). Possible execution error.")
                    return "failed"

            # Always read fresh state from memory for accurate card coordinates
            state = self._read_state()

            # Check if already won (earlier than expected, e.g. auto-complete)
            if state.is_won:
                self.games_won += 1
                self.log(f"\U0001f389 Game WON! ({move_count} moves executed)")
                return "won"

            # Log the move
            remaining = self.perfect_solver.moves_remaining
            self.vlog(f"[{move_count + 1}/{move_count + 1 + remaining}] {move}")

            # Execute the move with verification and retry logic
            old_hash = self._hash_state(state)
            success = False

            for attempt in range(max_retries):
                self.controller.execute_move(move, state)
                time.sleep(self.post_move_delay)

                # Re-read and verify the state actually changed
                new_state = self._read_state()
                new_hash = self._hash_state(new_state)

                if new_hash != old_hash:
                    success = True
                    state = new_state
                    break
                else:
                    if attempt < max_retries - 1:
                        self.vlog(f"  \u26a0 Move had no effect, retrying... "
                                 f"(attempt {attempt + 2}/{max_retries})")
                        # Re-read state for fresh coordinates before retry
                        state = self._read_state()
                        time.sleep(0.1)

            if not success:
                self.log(f"\u26a0 Move failed after {max_retries} attempts at "
                        f"step {move_count + 1}. Aborting solution.")
                return "failed"

            move_count += 1
            self.total_moves += 1

            # Flip any exposed face-down cards
            self._flip_exposed_cards(state)
            time.sleep(0.03 if self._fast else 0.15)

        return "failed"

    def _display_hidden_cards(self, state: GameState):
        """Display all face-down cards (verbose mode)."""
        print("  Hidden cards:")
        for t in state.tableau:
            hidden = [c for c in t.cards if c.face_down]
            if hidden:
                cards_str = " ".join(
                    f"{c.rank.display}{c.suit.symbol}" for c in hidden
                )
                print(f"    Tableau {t.tableau_index}: {cards_str}")
        stock_cards = " ".join(
            f"{c.rank.display}{c.suit.symbol}" for c in state.stock.cards
        )
        if stock_cards:
            print(f"    Stock: {stock_cards}")
        print()

    def _read_state(self) -> GameState:
        """Read game state with retry logic."""
        for attempt in range(3):
            try:
                state = self.reader.read_game_state()
                # Sanity check
                if state.total_cards != 52:
                    self.vlog(f"Warning: {state.total_cards} cards detected "
                             f"(expected 52), retrying...")
                    time.sleep(READ_RETRY_DELAY)
                    continue
                return state
            except MemoryReadError:
                if attempt < 2:
                    time.sleep(READ_RETRY_DELAY)
                else:
                    raise
        # If we get here, return whatever we got
        return self.reader.read_game_state()

    def _flip_exposed_cards(self, state: GameState):
        """
        Check all tableau columns for piles that have cards but no face-up
        card on top. Click the top card to flip it face-up.
        """
        for t in state.tableau:
            if t.is_empty:
                continue
            if t.top_card and t.top_card.face_down:
                self.vlog(f"Flipping exposed card on Tableau {t.tableau_index}")
                self.controller.flip_top_card(t)
                time.sleep(0.03 if self._fast else 0.2)

    @staticmethod
    def _hash_state(state: GameState) -> int:
        """Hash game state for change detection."""
        parts = []
        for pile in state.all_piles:
            pile_data = tuple(
                (c.card_id, c.face_down) for c in pile.cards
            )
            parts.append(pile_data)
        return hash(tuple(parts))

    def _cleanup(self):
        """Clean up resources."""
        if self.reader:
            self.reader.close()
        keyboard.unhook_all()

    def _print_stats(self):
        """Print session statistics."""
        self.log("")
        self.log("=== Session Stats ===")
        self.log(f"Games attempted:  {self.games_attempted}")
        self.log(f"Games solved:     {self.games_solved}")
        self.log(f"Games won:        {self.games_won}")
        self.log(f"Games unsolvable: {self.games_unsolvable}")
        if self.games_attempted > 0:
            solve_rate = (self.games_solved / self.games_attempted) * 100
            win_rate = (self.games_won / self.games_attempted) * 100
            self.log(f"Solve rate:       {solve_rate:.1f}%")
            self.log(f"Win rate:         {win_rate:.1f}%")
        self.log(f"Total moves:      {self.total_moves}")
        self.log(f"Total solve time: {self.total_solve_time:.2f}s")
        self.log(f"Total nodes:      {self.total_nodes:,}")
        if self.games_solved > 0:
            avg_time = self.total_solve_time / self.games_solved
            avg_nodes = self.total_nodes / self.games_solved
            self.log(f"Avg solve time:   {avg_time:.2f}s")
            self.log(f"Avg nodes/solve:  {avg_nodes:,.0f}")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Solitaire Bot 4 \u2014 Perfect-information Windows XP Solitaire player",
    )
    parser.add_argument(
        "--exe", default=DEFAULT_EXE_PATH,
        help=f"Path to sol.exe (default: {DEFAULT_EXE_PATH})",
    )
    parser.add_argument(
        "--speed", type=float, default=DEFAULT_MOVE_DELAY,
        help=f"Delay between moves in seconds (default: {DEFAULT_MOVE_DELAY})",
    )
    parser.add_argument(
        "--solve-timeout", type=float, default=DEFAULT_SOLVE_TIMEOUT,
        help=f"Max seconds to spend solving each game (default: {DEFAULT_SOLVE_TIMEOUT})",
    )
    parser.add_argument(
        "--max-stock-passes", type=int, default=DEFAULT_MAX_STOCK_PASSES,
        help=f"Max stock passes the solver considers (default: {DEFAULT_MAX_STOCK_PASSES})",
    )
    parser.add_argument(
        "--max-attempts", type=int, default=0,
        help="Max games to attempt (0 = unlimited, default: 0)",
    )
    parser.add_argument(
        "--fast", action="store_true",
        help="Run as fast as possible (minimal delays)",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Show detailed move log, board state, and hidden cards",
    )
    parser.add_argument(
        "--no-launch", action="store_true",
        help="Don't auto-launch sol.exe (must already be running)",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    bot = SolitaireBot(args)
    bot.start()


if __name__ == "__main__":
    main()
