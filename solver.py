"""
Solver / strategy engine for Klondike Solitaire.

Uses a priority-based heuristic approach:
  1. Move Aces and Twos to foundations immediately.
  2. Expose face-down cards (move cards off piles with hidden cards).
  3. Build foundation piles when safe.
  4. Move cards between tableau columns to create useful sequences.
  5. Use the stock/waste as a last resort.
  6. Avoid pointless moves (back-and-forth, King on empty with no gain, etc.).
"""

from dataclasses import dataclass
from enum import IntEnum
from typing import List, Optional, Tuple

from game_state import (
    Card, Pile, PileType, GameState, Suit, Rank,
)


class MoveType(IntEnum):
    """Types of moves the bot can make."""
    DRAW_STOCK = 0           # Draw card(s) from stock to waste
    WASTE_TO_FOUNDATION = 1  # Move top waste card to a foundation
    WASTE_TO_TABLEAU = 2     # Move top waste card to a tableau column
    TABLEAU_TO_FOUNDATION = 3  # Move top tableau card to a foundation
    TABLEAU_TO_TABLEAU = 4   # Move card(s) between tableau columns
    RECYCLE_WASTE = 5        # Flip waste back to stock (when stock is empty)


@dataclass
class Move:
    """Represents a single game move."""
    move_type: MoveType
    source: PileType           # Source pile
    dest: PileType             # Destination pile
    card: Optional[Card] = None  # The card being moved (for logging)
    num_cards: int = 1         # Number of cards moved (for tableau-to-tableau)
    priority: int = 0          # Higher = do this first

    def __str__(self) -> str:
        card_str = str(self.card) if self.card else "?"
        type_names = {
            MoveType.DRAW_STOCK: "Draw from stock",
            MoveType.WASTE_TO_FOUNDATION: f"Waste → Foundation: {card_str}",
            MoveType.WASTE_TO_TABLEAU: f"Waste → Tableau {self.dest - PileType.TABLEAU_0}: {card_str}",
            MoveType.TABLEAU_TO_FOUNDATION: f"Tableau {self.source - PileType.TABLEAU_0} → Foundation: {card_str}",
            MoveType.TABLEAU_TO_TABLEAU: f"Tableau {self.source - PileType.TABLEAU_0} → "
                                         f"Tableau {self.dest - PileType.TABLEAU_0}: "
                                         f"{card_str} ({self.num_cards} cards)",
            MoveType.RECYCLE_WASTE: "Recycle waste → stock",
        }
        return type_names.get(self.move_type, f"Unknown move: {self.move_type}")

    @property
    def reverse_key(self) -> Tuple:
        """Key that identifies the reverse of this move."""
        return (self.move_type, self.dest, self.source, self.num_cards)

    @property
    def forward_key(self) -> Tuple:
        """Key that identifies this move."""
        return (self.move_type, self.source, self.dest, self.num_cards)


class Solver:
    """
    Strategy engine for Klondike Solitaire.

    Priority-based approach rather than brute-force search.
    Falls back to stock draws when no good tableau/foundation moves exist.
    Tracks recent moves to prevent back-and-forth cycling.
    """

    def __init__(self, max_stock_passes: int = 10):
        self.max_stock_passes = max_stock_passes
        self.stock_pass_count = 0
        self.previous_states = set()
        self.moves_without_progress = 0
        self.max_moves_without_progress = 200
        # Track last N moves to prevent cycles
        self.recent_moves: List[Tuple] = []
        self.max_recent = 10

    def reset(self):
        """Reset solver state for a new game."""
        self.stock_pass_count = 0
        self.previous_states.clear()
        self.moves_without_progress = 0
        self.recent_moves.clear()

    def get_best_move(self, state: GameState) -> Optional[Move]:
        """
        Determine the best move to make given the current game state.
        Returns None if no moves are available (game is stuck).
        """
        if state.is_won:
            return None

        # Check for repeated state (stuck in loop)
        state_hash = self._hash_state(state)
        if state_hash in self.previous_states:
            self.moves_without_progress += 1
        else:
            self.previous_states.add(state_hash)
            self.moves_without_progress = 0

        if self.moves_without_progress > self.max_moves_without_progress:
            return None  # Stuck

        # Generate all legal moves and pick the best one
        moves = self._generate_all_moves(state)
        if not moves:
            return None

        # Filter out moves that would reverse a recent move (back-and-forth)
        non_reversing = [m for m in moves if not self._is_reverse_of_recent(m)]
        candidates = non_reversing if non_reversing else moves

        # Sort by priority (descending) and return the best
        candidates.sort(key=lambda m: m.priority, reverse=True)
        chosen = candidates[0]

        # Record this move
        self.recent_moves.append(chosen.forward_key)
        if len(self.recent_moves) > self.max_recent:
            self.recent_moves.pop(0)

        return chosen

    def _is_reverse_of_recent(self, move: Move) -> bool:
        """Check if this move reverses any of the last few moves."""
        if move.move_type != MoveType.TABLEAU_TO_TABLEAU:
            return False
        reverse = move.reverse_key
        # Check if the reverse of this move was done recently
        for recent in self.recent_moves[-4:]:
            if recent == reverse:
                return True
        return False

    def _generate_all_moves(self, state: GameState) -> List[Move]:
        """Generate all legal moves with priorities."""
        moves = []

        # 1. Foundation moves (highest priority for safe cards)
        moves.extend(self._foundation_moves(state))

        # 2. Tableau-to-tableau moves
        moves.extend(self._tableau_to_tableau_moves(state))

        # 3. Waste-to-tableau moves
        moves.extend(self._waste_to_tableau_moves(state))

        # 4. Draw from stock (low priority)
        moves.extend(self._stock_moves(state))

        return moves

    def _foundation_moves(self, state: GameState) -> List[Move]:
        """Generate moves to foundations. Safe cards get max priority."""
        moves = []

        # Check waste to foundation
        if state.waste.top_card and not state.waste.top_card.face_down:
            card = state.waste.top_card
            dest = state.foundation_accepts(card)
            if dest:
                priority = self._foundation_priority(card, state)
                # Penalize if this card is a useful tableau target for
                # exposing hidden cards elsewhere
                if self._card_useful_as_tableau_target(card, state):
                    priority = min(priority, 40)
                moves.append(Move(
                    move_type=MoveType.WASTE_TO_FOUNDATION,
                    source=PileType.WASTE,
                    dest=dest.pile_type,
                    card=card,
                    priority=priority,
                ))

        # Check tableau to foundation
        for t in state.tableau:
            if t.top_card and not t.top_card.face_down:
                card = t.top_card
                dest = state.foundation_accepts(card)
                if dest:
                    priority = self._foundation_priority(card, state)
                    # Bonus: if moving reveals a face-down card
                    if len(t.cards) > 1 and t.cards[-2].face_down:
                        priority += 20
                    # Penalize if this card is a useful tableau target for
                    # exposing hidden cards elsewhere (but not if moving it
                    # ALSO reveals a hidden card in THIS pile)
                    elif self._card_useful_as_tableau_target(card, state):
                        priority = min(priority, 40)
                    moves.append(Move(
                        move_type=MoveType.TABLEAU_TO_FOUNDATION,
                        source=t.pile_type,
                        dest=dest.pile_type,
                        card=card,
                        priority=priority,
                    ))

        return moves

    def _card_useful_as_tableau_target(self, card: Card, state: GameState) -> bool:
        """
        Check if keeping this card on the tableau would allow another card
        to be placed on it, exposing a hidden card.

        E.g., 3♦ on tableau could receive 2♣ from a column with hidden cards.
        Moving 3♦ to the foundation would waste that opportunity.
        """
        # What card could be placed on this one?  It would need to be
        # rank-1, opposite color.
        needed_rank = card.rank - 1
        if needed_rank < 0:
            return False  # Aces can't receive anything

        needed_color = "red" if card.is_black else "black"

        for t in state.tableau:
            if t.is_empty:
                continue
            top = t.top_card
            if (top and not top.face_down and
                    top.rank == needed_rank and
                    top.color == needed_color):
                # This card could be placed on ours. Is there a hidden card
                # underneath it that would be exposed?
                if len(t.cards) >= 2 and t.cards[-2].face_down:
                    return True

        return False

    def _foundation_priority(self, card: Card, state: GameState) -> int:
        """
        Calculate priority for moving a card to the foundation.
        Aces and Twos are always safe to move.
        """
        rank_val = card.rank.value

        if rank_val == 0:
            return 1000  # Aces: always move immediately

        if rank_val == 1:
            return 900  # Twos: always safe

        min_opp = self._min_opposite_foundation_rank(card, state)
        if min_opp >= rank_val - 1:
            return 800 - rank_val  # Safe to move

        return 100 - rank_val  # Possibly useful but risky

    def _min_opposite_foundation_rank(self, card: Card, state: GameState) -> int:
        """Min rank on foundations of opposite color. -1 if any is empty."""
        opposite_color = "red" if card.is_black else "black"
        opp_foundations = [f for f in state.foundations
                          if f.top_card and f.top_card.color == opposite_color]
        if len(opp_foundations) < 2:
            return -1
        return min(f.top_card.rank.value for f in opp_foundations)

    def _tableau_to_tableau_moves(self, state: GameState) -> List[Move]:
        """Generate tableau-to-tableau moves."""
        moves = []

        for src in state.tableau:
            if src.is_empty:
                continue

            # Find the deepest face-up card
            face_up_start = None
            for i, card in enumerate(src.cards):
                if not card.face_down:
                    face_up_start = i
                    break

            if face_up_start is None:
                continue

            # Try moving subsequences starting from the deepest face-up card
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
                            can_place = True
                    elif (dst.top_card and
                          not dst.top_card.face_down and
                          dst.top_card.color != card.color and
                          dst.top_card.rank == card.rank + 1):
                        can_place = True

                    if can_place:
                        priority = self._tableau_move_priority(
                            src, dst, start_idx, num_cards, state
                        )
                        if priority > 0:
                            moves.append(Move(
                                move_type=MoveType.TABLEAU_TO_TABLEAU,
                                source=src.pile_type,
                                dest=dst.pile_type,
                                card=card,
                                num_cards=num_cards,
                                priority=priority,
                            ))

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

    def _tableau_move_priority(self, src: Pile, dst: Pile,
                                start_idx: int, num_cards: int,
                                state: GameState) -> int:
        """
        Calculate priority for a tableau-to-tableau move.
        Returns 0 or negative to skip useless moves.

        Key principles:
        - Exposing face-down cards is the #1 goal
        - Prefer moving FROM columns with more hidden cards
        - Avoid moving to columns with more hidden cards than source
        - Don't move a King off an empty base for no reason
        - Don't just shuffle cards between fully-visible columns
        - Don't move cards between tableau columns unless the move
          exposes a hidden card, enables a foundation play, or
          consolidates cards onto an empty column
        """
        card = src.cards[start_idx]
        priority = 50  # Base priority

        src_hidden = src.face_down_count
        dst_hidden = dst.face_down_count

        exposes_hidden = start_idx > 0 and src.cards[start_idx - 1].face_down

        # === HARD GATE: moves that don't expose hidden cards must justify themselves ===
        # Without this, the bot shuffles cards back and forth endlessly
        # (e.g., 10♦+9♠ between two Jacks on different tableau columns).
        if not exposes_hidden:
            # Check if this move enables a foundation play:
            # the card exposed underneath the moved sequence can go to foundation
            enables_foundation = False
            if start_idx > 0:
                card_below = src.cards[start_idx - 1]
                if not card_below.face_down and state.foundation_accepts(card_below):
                    enables_foundation = True

            # Check if this move empties the source column (freeing a slot for a King)
            empties_column = (start_idx == 0)

            if enables_foundation:
                # Good — allow with high priority
                priority += 150
            elif empties_column and card.rank == Rank.KING:
                # Moving a King to empty column doesn't free source (King IS the base)
                return 0
            elif empties_column:
                # Emptying a column is valuable if we have Kings to place
                priority += 100
            else:
                # No hidden card exposed, no foundation enabled, doesn't empty column.
                # This is pointless shuffling — block it.
                return 0

            return max(priority, 1)

        # === From here on, the move DOES expose a hidden card ===
        priority += 200
        # More hidden cards in source = more value in exposing one
        priority += src_hidden * 10

        # === KING TO EMPTY COLUMN (exposing hidden) ===
        if card.rank == Rank.KING and dst.is_empty:
            priority += 50

        # === PREFER MOVING LARGER SEQUENCES (when exposing) ===
        priority += num_cards * 2

        return max(priority, 1)

    def _waste_to_tableau_moves(self, state: GameState) -> List[Move]:
        """Generate waste-to-tableau moves."""
        moves = []

        if not state.waste.top_card or state.waste.top_card.face_down:
            return moves

        card = state.waste.top_card
        targets = state.tableau_accepts(card)

        for dst in targets:
            priority = 60
            if not dst.is_empty:
                priority += 10
                # Prefer placing on columns with fewer hidden cards
                priority -= dst.face_down_count
            moves.append(Move(
                move_type=MoveType.WASTE_TO_TABLEAU,
                source=PileType.WASTE,
                dest=dst.pile_type,
                card=card,
                priority=priority,
            ))

        return moves

    def _stock_moves(self, state: GameState) -> List[Move]:
        """Generate stock draw / recycle moves."""
        moves = []

        if state.stock.cards:
            moves.append(Move(
                move_type=MoveType.DRAW_STOCK,
                source=PileType.STOCK,
                dest=PileType.WASTE,
                priority=10,
            ))
        elif state.waste.cards:
            if self.stock_pass_count < self.max_stock_passes:
                moves.append(Move(
                    move_type=MoveType.RECYCLE_WASTE,
                    source=PileType.WASTE,
                    dest=PileType.STOCK,
                    priority=5,
                ))

        return moves

    def notify_stock_recycled(self):
        """Called when the waste is recycled back to stock."""
        self.stock_pass_count += 1

    def _hash_state(self, state: GameState) -> int:
        """Create a hash of the game state for cycle detection."""
        parts = []
        for pile in state.all_piles:
            pile_data = tuple(
                (c.card_id, c.face_down) for c in pile.cards
            )
            parts.append(pile_data)
        return hash(tuple(parts))

    def is_stuck(self) -> bool:
        """Check if the solver has determined the game is unsolvable."""
        return (self.stock_pass_count >= self.max_stock_passes or
                self.moves_without_progress >= self.max_moves_without_progress)
