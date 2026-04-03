"""
Perfect-information solver for Klondike Solitaire (Bot4).

Since we can read ALL cards from memory (including face-down ones),
this solver determines the complete winning move sequence BEFORE
making any moves. Uses DFS with aggressive pruning, move ordering,
and a permanent transposition table.

Handles both draw-1 and draw-3 modes. Draw-3 is significantly harder
to solve due to restricted stock access — only every 3rd card is
accessible per stock pass, requiring multiple recycles to reach all cards.

If no solution is found within the time limit, the game is declared
unsolvable and the bot redeals.
"""

import time
from typing import List, Optional, Tuple, Set, Dict

from game_state import (
    Card, Pile, PileType, GameState, Suit, Rank,
)
from solver import Move, MoveType


class SolveResult:
    """Result of a solve attempt."""

    def __init__(self, solved: bool, moves: Optional[List[Move]] = None,
                 nodes_explored: int = 0, elapsed: float = 0.0,
                 reason: str = ""):
        self.solved = solved
        self.moves = moves or []
        self.nodes_explored = nodes_explored
        self.elapsed = elapsed
        self.reason = reason

    def __str__(self) -> str:
        if self.solved:
            return (f"Solved in {len(self.moves)} moves "
                    f"({self.nodes_explored} nodes, {self.elapsed:.2f}s)")
        return f"Unsolvable: {self.reason} ({self.nodes_explored} nodes, {self.elapsed:.2f}s)"


class PerfectSolver:
    """
    Perfect-information Klondike solver.

    Uses DFS with:
    - Permanent transposition table (no backtrack removal)
    - Forced moves (auto-play safe foundation moves)
    - Move ordering (foundation > expose hidden > tableau > stock)
    - Aggressive pruning for draw-3 stock access
    - Depth limiting to prevent infinite exploration
    """

    def __init__(self, timeout: float = 30.0, max_stock_passes: int = 5,
                 verbose: bool = False):
        self.timeout = timeout
        self.max_stock_passes = max_stock_passes
        self.verbose = verbose

        # Stats
        self.nodes_explored = 0
        self.start_time = 0.0
        # Permanent transposition table: hash -> best_foundation_count
        # We only skip a state if we've seen it with >= foundation cards
        self.visited: Dict[int, int] = {}
        self.max_depth = 800  # Safety limit (draw-3 solutions can be long)

    def solve(self, state: GameState) -> SolveResult:
        """
        Attempt to find a complete winning move sequence.
        Returns a SolveResult with the move list if solvable.
        """
        self.nodes_explored = 0
        self.start_time = time.time()
        self.visited = {}

        # Apply forced moves first (Aces, safe Twos)
        state = state.clone()
        pre_moves = self._apply_forced_moves(state)

        if state.is_won:
            elapsed = time.time() - self.start_time
            return SolveResult(True, pre_moves, self.nodes_explored, elapsed)

        # Run DFS
        result = self._dfs(state, 0, 0, None)

        elapsed = time.time() - self.start_time

        if result is not None:
            all_moves = pre_moves + result
            return SolveResult(True, all_moves, self.nodes_explored, elapsed)

        reason = "timeout" if self._timed_out() else "exhausted search space"
        return SolveResult(False, None, self.nodes_explored, elapsed, reason)

    def _timed_out(self) -> bool:
        return (time.time() - self.start_time) >= self.timeout

    def _foundation_count(self, state: GameState) -> int:
        """Total cards on foundations."""
        return sum(len(f.cards) for f in state.foundations)

    def _dfs(self, state: GameState, depth: int,
             stock_passes: int, last_move: Optional[Move],
             recent_tab_moves: Optional[List[Move]] = None) -> Optional[List[Move]]:
        """
        Depth-first search with pruning.
        Returns the move sequence from this point to victory, or None.
        """
        if recent_tab_moves is None:
            recent_tab_moves = []

        self.nodes_explored += 1

        # Time check every 2000 nodes
        if self.nodes_explored % 2000 == 0 and self._timed_out():
            return None

        # Depth limit
        if depth > self.max_depth:
            return None

        # Check win
        if state.is_won:
            return []

        # Transposition check with foundation-count dominance:
        # Only skip if we've visited this state with at least as many
        # foundation cards (meaning the previous visit was at least as good).
        h = state.state_hash()
        fc = self._foundation_count(state)
        prev_fc = self.visited.get(h)
        if prev_fc is not None and prev_fc >= fc:
            return None
        self.visited[h] = fc

        # Generate and order moves, filtering reversals of recent tableau moves
        moves = self._generate_ordered_moves(state, stock_passes)
        moves = [m for m in moves
                 if not self._is_reverse_of_recent(m, recent_tab_moves)]

        for move in moves:
            new_state = state.apply_move(move)

            # Apply forced moves after this move
            forced = self._apply_forced_moves(new_state)

            new_passes = stock_passes
            if move.move_type == MoveType.RECYCLE_WASTE:
                new_passes += 1
                if new_passes > self.max_stock_passes:
                    continue

            # Update recent tableau moves list (keep last 6)
            # Clear the list when foundation progress happens (forced moves
            # moved cards to foundation), since that indicates real progress
            # and previously-reversed moves may now be legitimately needed.
            new_recent_tab = recent_tab_moves
            if forced:  # Foundation progress happened
                new_recent_tab = []
            if move.move_type == MoveType.TABLEAU_TO_TABLEAU:
                new_recent_tab = (new_recent_tab + [move])[-6:]

            result = self._dfs(new_state, depth + 1, new_passes, move, new_recent_tab)
            if result is not None:
                return [move] + forced + result

            if self._timed_out():
                return None

        return None

    @staticmethod
    def _is_reverse_of_recent(move: Move,
                              recent_tab_moves: List[Move]) -> bool:
        """
        Check if `move` reverses any recent tableau-to-tableau move.
        This catches both direct and interleaved reversal patterns like:
          A→B, C→D, B→A  (reversal of first, interleaved with second)
          A→B, draw, B→A  (reversal across stock draw)
        """
        if move.move_type != MoveType.TABLEAU_TO_TABLEAU:
            return False

        for prev in recent_tab_moves:
            if (move.source == prev.dest and
                    move.dest == prev.source and
                    move.num_cards == prev.num_cards):
                return True

        return False

    def _apply_forced_moves(self, state: GameState) -> List[Move]:
        """
        Apply moves that are always correct (never need backtracking):
        - Move Aces to foundations
        - Move Twos to foundations when the Ace of same suit is already there
        - Higher cards when both opposite-color foundations are high enough

        Modifies state in-place and returns the list of forced moves applied.
        """
        forced = []
        changed = True

        while changed:
            changed = False

            # Check waste
            if state.waste.cards and not state.waste.top_card.face_down:
                card = state.waste.top_card
                if self._is_auto_foundation_card(card, state):
                    dest = state.foundation_accepts(card)
                    if dest:
                        move = Move(
                            move_type=MoveType.WASTE_TO_FOUNDATION,
                            source=PileType.WASTE,
                            dest=dest.pile_type,
                            card=card,
                        )
                        state.waste.cards.pop()
                        dest.cards.append(card)
                        forced.append(move)
                        changed = True
                        continue

            # Check tableau
            for t in state.tableau:
                if t.is_empty:
                    continue
                card = t.top_card
                if card and not card.face_down and self._is_auto_foundation_card(card, state):
                    dest = state.foundation_accepts(card)
                    if dest:
                        move = Move(
                            move_type=MoveType.TABLEAU_TO_FOUNDATION,
                            source=t.pile_type,
                            dest=dest.pile_type,
                            card=card,
                        )
                        t.cards.pop()
                        card.face_down = False
                        dest.cards.append(card)
                        # Flip newly exposed card
                        if t.cards and t.cards[-1].face_down:
                            t.cards[-1].face_down = False
                        forced.append(move)
                        changed = True
                        break  # restart outer loop

        return forced

    def _is_auto_foundation_card(self, card: Card, state: GameState) -> bool:
        """
        Check if a card can be safely auto-moved to foundation.

        Safe to auto-move:
        - Aces (always)
        - Twos (always — the card that needs the Ace is already on foundation)
        - Higher cards IF both opposite-color foundations have rank >= card.rank - 1
          (meaning no card of opposite color still needs to be placed ON this card)
        """
        if state.foundation_accepts(card) is None:
            return False

        rank_val = card.rank.value

        if rank_val <= 1:  # Ace or Two
            return True

        # For rank 3+, check if it's safe
        opposite_color = "red" if card.is_black else "black"
        min_opp = 99
        opp_count = 0
        for f in state.foundations:
            if f.cards and f.cards[0].color == opposite_color:
                opp_count += 1
                min_opp = min(min_opp, f.top_card.rank.value)

        if opp_count < 2:
            return False

        return min_opp >= rank_val - 1

    def _generate_ordered_moves(self, state: GameState,
                                 stock_passes: int) -> List[Move]:
        """
        Generate all legal moves, ordered by priority for DFS efficiency.

        Order:
        1. Foundation moves (non-forced — rank 3+ that aren't auto-safe)
        2. Tableau moves that expose hidden cards
        3. Tableau moves that empty columns
        4. Other tableau-to-tableau moves
        5. Waste to tableau
        6. Draw from stock
        7. Recycle waste
        """
        moves: List[Tuple[int, Move]] = []

        # Foundation moves from tableau
        for t in state.tableau:
            if t.is_empty:
                continue
            card = t.top_card
            if card and not card.face_down:
                dest = state.foundation_accepts(card)
                if dest:
                    exposes = len(t.cards) > 1 and t.cards[-2].face_down
                    priority = 900 if exposes else 800
                    moves.append((priority, Move(
                        move_type=MoveType.TABLEAU_TO_FOUNDATION,
                        source=t.pile_type,
                        dest=dest.pile_type,
                        card=card,
                    )))

        # Foundation moves from waste
        if state.waste.cards and not state.waste.top_card.face_down:
            card = state.waste.top_card
            dest = state.foundation_accepts(card)
            if dest:
                moves.append((750, Move(
                    move_type=MoveType.WASTE_TO_FOUNDATION,
                    source=PileType.WASTE,
                    dest=dest.pile_type,
                    card=card,
                )))

        # Tableau-to-tableau moves
        tab_moves = self._tableau_to_tableau_moves(state)
        moves.extend(tab_moves)

        # Waste to tableau
        if state.waste.cards and not state.waste.top_card.face_down:
            card = state.waste.top_card
            for dst in state.tableau:
                can_place = False
                if dst.is_empty:
                    if card.rank == Rank.KING:
                        can_place = True
                elif (dst.top_card and
                      not dst.top_card.face_down and
                      dst.top_card.color != card.color and
                      dst.top_card.rank == card.rank + 1):
                    can_place = True

                if can_place:
                    # Higher priority if it helps expose cards
                    priority = 300
                    moves.append((priority, Move(
                        move_type=MoveType.WASTE_TO_TABLEAU,
                        source=PileType.WASTE,
                        dest=dst.pile_type,
                        card=card,
                    )))

        # Waste to foundation (already handled above)

        # Draw from stock
        if state.stock.cards:
            moves.append((100, Move(
                move_type=MoveType.DRAW_STOCK,
                source=PileType.STOCK,
                dest=PileType.WASTE,
            )))

        # Recycle waste (only if stock is empty and we haven't exceeded passes)
        if not state.stock.cards and state.waste.cards:
            if stock_passes < self.max_stock_passes:
                moves.append((50, Move(
                    move_type=MoveType.RECYCLE_WASTE,
                    source=PileType.WASTE,
                    dest=PileType.STOCK,
                )))

        # Sort by priority descending
        moves.sort(key=lambda x: x[0], reverse=True)
        return [m for _, m in moves]

    def _tableau_to_tableau_moves(self, state: GameState) -> List[Tuple[int, Move]]:
        """Generate tableau-to-tableau moves with priorities."""
        moves: List[Tuple[int, Move]] = []

        for src in state.tableau:
            if src.is_empty:
                continue

            # Find deepest face-up card
            face_up_start = None
            for i, card in enumerate(src.cards):
                if not card.face_down:
                    face_up_start = i
                    break
            if face_up_start is None:
                continue

            for start_idx in range(face_up_start, len(src.cards)):
                card = src.cards[start_idx]
                num_cards = len(src.cards) - start_idx

                if not self._is_valid_sequence(src.cards[start_idx:]):
                    break

                for dst in state.tableau:
                    if dst.pile_type == src.pile_type:
                        continue

                    can_place = False
                    if dst.is_empty:
                        if card.rank == Rank.KING:
                            # Only move King to empty if it exposes a hidden card
                            if start_idx > 0:
                                can_place = True
                    elif (dst.top_card and
                          not dst.top_card.face_down and
                          dst.top_card.color != card.color and
                          dst.top_card.rank == card.rank + 1):
                        can_place = True

                    if can_place:
                        exposes = start_idx > 0 and src.cards[start_idx - 1].face_down
                        empties = start_idx == 0

                        if exposes:
                            priority = 700 + num_cards
                        elif empties:
                            priority = 600
                        else:
                            # Non-exposing, non-emptying tableau move.
                            # Check if it enables a foundation play on the
                            # card below the moved sequence.
                            enables_foundation = False
                            if start_idx > 0:
                                card_below = src.cards[start_idx - 1]
                                if (not card_below.face_down and
                                        state.foundation_accepts(card_below)):
                                    enables_foundation = True

                            if enables_foundation:
                                priority = 650
                            else:
                                # Low priority — might be needed for structure
                                # but should be tried after everything else.
                                priority = 200 + num_cards

                        moves.append((priority, Move(
                            move_type=MoveType.TABLEAU_TO_TABLEAU,
                            source=src.pile_type,
                            dest=dst.pile_type,
                            card=card,
                            num_cards=num_cards,
                        )))

        return moves

    def _is_valid_sequence(self, cards: List[Card]) -> bool:
        """Check if cards form a valid descending alternating-color sequence."""
        for i in range(1, len(cards)):
            if cards[i].face_down:
                return False
            if cards[i].color == cards[i - 1].color:
                return False
            if cards[i].rank != cards[i - 1].rank - 1:
                return False
        return True


class PerfectSolverWrapper:
    """
    Wrapper that integrates the perfect solver with the bot's main loop.

    Pre-computes the winning move sequence and returns moves one at a time.
    """

    def __init__(self, timeout: float = 30.0, max_stock_passes: int = 5,
                 verbose: bool = False):
        self.solver = PerfectSolver(
            timeout=timeout,
            max_stock_passes=max_stock_passes,
            verbose=verbose,
        )
        self.move_queue: List[Move] = []
        self.current_index: int = 0
        self.last_result: Optional[SolveResult] = None

    def solve(self, state: GameState) -> SolveResult:
        """
        Attempt to solve the game. If successful, the move queue is loaded
        and get_next_move() will return moves in sequence.
        """
        result = self.solver.solve(state)
        self.last_result = result

        if result.solved:
            self.move_queue = result.moves
            self.current_index = 0
        else:
            self.move_queue = []
            self.current_index = 0

        return result

    def get_next_move(self) -> Optional[Move]:
        """Return the next move in the pre-computed sequence, or None if done."""
        if self.current_index >= len(self.move_queue):
            return None
        move = self.move_queue[self.current_index]
        self.current_index += 1
        return move

    @property
    def moves_remaining(self) -> int:
        return len(self.move_queue) - self.current_index

    @property
    def is_solved(self) -> bool:
        return self.last_result is not None and self.last_result.solved
