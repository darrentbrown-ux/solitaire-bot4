"""
Card and game state models for Windows XP Solitaire (Klondike).

Card encoding from sol.exe memory:
  - 12 bytes per card
  - WORD at offset 0: flags | card_id
    - bits 0-5: card identity (0-51)
    - bit 15 (0x8000): face-down flag
  - DWORD at offset 4: x coordinate
  - DWORD at offset 8: y coordinate

Card identity:
  card_id / 4 = rank (0=Ace, 1=Two, ..., 12=King)
  card_id % 4 = suit (0=Clubs, 1=Diamonds, 2=Hearts, 3=Spades)

Bot4 extensions:
  - Card.clone() for deep copy
  - Pile.clone() for deep copy
  - GameState.clone() for search tree exploration
  - GameState.apply_move() for simulation
  - GameState.state_hash() for transposition table
"""

import copy
from dataclasses import dataclass, field
from enum import IntEnum
from typing import List, Optional, Tuple


class Suit(IntEnum):
    CLUBS = 0
    DIAMONDS = 1
    HEARTS = 2
    SPADES = 3

    @property
    def symbol(self) -> str:
        return ["♣", "♦", "♥", "♠"][self.value]

    @property
    def color(self) -> str:
        """Red for diamonds/hearts, black for clubs/spades."""
        return "red" if self.value in (1, 2) else "black"


class Rank(IntEnum):
    ACE = 0
    TWO = 1
    THREE = 2
    FOUR = 3
    FIVE = 4
    SIX = 5
    SEVEN = 6
    EIGHT = 7
    NINE = 8
    TEN = 9
    JACK = 10
    QUEEN = 11
    KING = 12

    @property
    def display(self) -> str:
        names = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]
        return names[self.value]


FACE_UP_FLAG = 0x8000    # bit 15 SET = face UP (not down!)
CARD_ID_MASK = 0x3F      # bits 0-5 hold the card identity (0-51)


@dataclass
class Card:
    """Represents a single playing card."""
    card_id: int       # 0-51
    face_down: bool    # True if card is face-down
    x: int = 0         # screen x coordinate
    y: int = 0         # screen y coordinate

    @property
    def suit(self) -> Suit:
        return Suit(self.card_id % 4)

    @property
    def rank(self) -> Rank:
        return Rank(self.card_id // 4)

    @property
    def color(self) -> str:
        return self.suit.color

    @property
    def is_red(self) -> bool:
        return self.color == "red"

    @property
    def is_black(self) -> bool:
        return self.color == "black"

    def clone(self) -> "Card":
        """Return a deep copy of this card (screen coords preserved)."""
        return Card(card_id=self.card_id, face_down=self.face_down, x=self.x, y=self.y)

    def __str__(self) -> str:
        if self.face_down:
            return "[??]"
        return f"{self.rank.display}{self.suit.symbol}"

    def __repr__(self) -> str:
        return f"Card({self.rank.display}{self.suit.symbol}, {'down' if self.face_down else 'up'})"

    def __eq__(self, other):
        if not isinstance(other, Card):
            return False
        return self.card_id == other.card_id

    def __hash__(self):
        return hash(self.card_id)

    @staticmethod
    def from_memory(word: int, x: int = 0, y: int = 0) -> "Card":
        """Create a Card from the raw WORD value read from sol.exe memory.

        Bit 15 (0x8000) is the face-UP flag:
          SET (1) = card is face up
          CLEAR (0) = card is face down
        Bits 0-5 = card identity (0-51).
        """
        card_id = word & CARD_ID_MASK
        face_down = not bool(word & FACE_UP_FLAG)  # inverted: bit set = face UP
        return Card(card_id=card_id, face_down=face_down, x=x, y=y)


class PileType(IntEnum):
    STOCK = 0
    WASTE = 1
    FOUNDATION_0 = 2
    FOUNDATION_1 = 3
    FOUNDATION_2 = 4
    FOUNDATION_3 = 5
    TABLEAU_0 = 6
    TABLEAU_1 = 7
    TABLEAU_2 = 8
    TABLEAU_3 = 9
    TABLEAU_4 = 10
    TABLEAU_5 = 11
    TABLEAU_6 = 12


@dataclass
class Pile:
    """Represents a pile of cards (stock, waste, foundation, or tableau column)."""
    pile_type: PileType
    cards: List[Card] = field(default_factory=list)
    x: int = 0
    y: int = 0

    def clone(self) -> "Pile":
        """Return a deep copy of this pile."""
        return Pile(
            pile_type=self.pile_type,
            cards=[c.clone() for c in self.cards],
            x=self.x,
            y=self.y,
        )

    @property
    def is_empty(self) -> bool:
        return len(self.cards) == 0

    @property
    def top_card(self) -> Optional[Card]:
        """The top (last) card, or None if empty."""
        return self.cards[-1] if self.cards else None

    @property
    def face_up_cards(self) -> List[Card]:
        """All face-up cards in order (bottom to top)."""
        return [c for c in self.cards if not c.face_down]

    @property
    def face_down_count(self) -> int:
        return sum(1 for c in self.cards if c.face_down)

    @property
    def is_stock(self) -> bool:
        return self.pile_type == PileType.STOCK

    @property
    def is_waste(self) -> bool:
        return self.pile_type == PileType.WASTE

    @property
    def is_foundation(self) -> bool:
        return PileType.FOUNDATION_0 <= self.pile_type <= PileType.FOUNDATION_3

    @property
    def is_tableau(self) -> bool:
        return PileType.TABLEAU_0 <= self.pile_type <= PileType.TABLEAU_6

    @property
    def tableau_index(self) -> int:
        """0-based index for tableau columns (0-6)."""
        assert self.is_tableau
        return self.pile_type - PileType.TABLEAU_0

    @property
    def foundation_index(self) -> int:
        """0-based index for foundations (0-3)."""
        assert self.is_foundation
        return self.pile_type - PileType.FOUNDATION_0

    def __str__(self) -> str:
        name_map = {
            PileType.STOCK: "Stock",
            PileType.WASTE: "Waste",
        }
        if self.is_foundation:
            name = f"Foundation {self.foundation_index}"
        elif self.is_tableau:
            name = f"Tableau {self.tableau_index}"
        else:
            name = name_map.get(self.pile_type, "Unknown")
        cards_str = " ".join(str(c) for c in self.cards)
        return f"{name}: [{cards_str}]"


@dataclass
class GameState:
    """Complete state of a Klondike Solitaire game."""
    stock: Pile
    waste: Pile
    foundations: List[Pile]   # 4 foundation piles
    tableau: List[Pile]      # 7 tableau columns
    draw_count: int = 1      # 1 or 3 cards drawn at a time

    # ---- Bot4 extensions ----

    def clone(self) -> "GameState":
        """Return a deep copy of the game state for search tree exploration."""
        return GameState(
            stock=self.stock.clone(),
            waste=self.waste.clone(),
            foundations=[f.clone() for f in self.foundations],
            tableau=[t.clone() for t in self.tableau],
            draw_count=self.draw_count,
        )

    def state_hash(self) -> int:
        """
        Efficient hashable representation of the game state.

        Encodes card positions as a tuple of (pile_index, card_id, face_down)
        sorted by card_id so the hash is canonical regardless of pile order
        within the same logical group.

        For the solver simulation all face-down cards are actually KNOWN,
        so their face_down flag is meaningful for move legality (flip tracking)
        but not for card identity.
        """
        # Encode each pile as a compact tuple
        parts: List[Tuple] = []
        for pile in self.all_piles:
            pile_tuple = tuple((c.card_id, c.face_down) for c in pile.cards)
            parts.append(pile_tuple)
        return hash(tuple(parts))

    def apply_move(self, move: "Move") -> "GameState":  # type: ignore[name-defined]
        """
        Apply a move to a CLONE of this state and return the new state.
        Does NOT modify self. Used by the perfect solver for simulation.

        Handles:
          - DRAW_STOCK: move top stock card to waste (face-up)
          - RECYCLE_WASTE: move all waste cards back to stock (face-down)
          - WASTE_TO_FOUNDATION: move waste top to foundation
          - WASTE_TO_TABLEAU: move waste top to tableau column
          - TABLEAU_TO_FOUNDATION: move tableau top to foundation
          - TABLEAU_TO_TABLEAU: move N cards from src to dst tableau
        """
        from solver import MoveType  # local import to avoid circular deps

        new_state = self.clone()
        mt = move.move_type

        if mt == MoveType.DRAW_STOCK:
            if new_state.stock.cards:
                # Draw draw_count cards (or fewer if stock is smaller)
                num_to_draw = min(new_state.draw_count, len(new_state.stock.cards))
                for _ in range(num_to_draw):
                    card = new_state.stock.cards.pop()
                    card.face_down = False
                    new_state.waste.cards.append(card)

        elif mt == MoveType.RECYCLE_WASTE:
            # Flip waste back to stock in reverse order (face-down)
            while new_state.waste.cards:
                card = new_state.waste.cards.pop()
                card.face_down = True
                new_state.stock.cards.append(card)

        elif mt == MoveType.WASTE_TO_FOUNDATION:
            if new_state.waste.cards:
                card = new_state.waste.cards.pop()
                card.face_down = False
                dest_f = new_state._pile_by_type(move.dest)
                dest_f.cards.append(card)

        elif mt == MoveType.WASTE_TO_TABLEAU:
            if new_state.waste.cards:
                card = new_state.waste.cards.pop()
                card.face_down = False
                dest_t = new_state._pile_by_type(move.dest)
                dest_t.cards.append(card)

        elif mt == MoveType.TABLEAU_TO_FOUNDATION:
            src_t = new_state._pile_by_type(move.source)
            if src_t.cards:
                card = src_t.cards.pop()
                card.face_down = False
                # Flip newly exposed card (already KNOWN in bot4)
                if src_t.cards and src_t.cards[-1].face_down:
                    src_t.cards[-1].face_down = False
                dest_f = new_state._pile_by_type(move.dest)
                dest_f.cards.append(card)

        elif mt == MoveType.TABLEAU_TO_TABLEAU:
            src_t = new_state._pile_by_type(move.source)
            dst_t = new_state._pile_by_type(move.dest)
            n = move.num_cards
            if len(src_t.cards) >= n:
                seq = src_t.cards[-n:]
                src_t.cards = src_t.cards[:-n]
                for card in seq:
                    card.face_down = False
                dst_t.cards.extend(seq)
                # Flip newly exposed card in source
                if src_t.cards and src_t.cards[-1].face_down:
                    src_t.cards[-1].face_down = False

        return new_state

    def _pile_by_type(self, pile_type: "PileType") -> Pile:
        """Return the pile matching the given PileType."""
        for pile in self.all_piles:
            if pile.pile_type == pile_type:
                return pile
        raise KeyError(f"No pile with type {pile_type}")

    # ---- Original GameState methods ----

    @property
    def all_piles(self) -> List[Pile]:
        return [self.stock, self.waste] + self.foundations + self.tableau

    @property
    def is_won(self) -> bool:
        """Game is won when all 4 foundations have 13 cards (Ace through King)."""
        return all(len(f.cards) == 13 for f in self.foundations)

    @property
    def total_cards(self) -> int:
        return sum(len(p.cards) for p in self.all_piles)

    def foundation_for_suit(self, suit: Suit) -> Optional[Pile]:
        """Find the foundation pile that already has cards of this suit,
        or an empty one if none started yet."""
        for f in self.foundations:
            if f.cards and f.cards[0].suit == suit:
                return f
        # Return first empty foundation
        for f in self.foundations:
            if f.is_empty:
                return f
        return None

    def foundation_accepts(self, card: Card) -> Optional[Pile]:
        """Return the foundation pile that can accept this card, or None."""
        # Must be an Ace on empty, or next rank of same suit
        for f in self.foundations:
            if f.is_empty:
                if card.rank == Rank.ACE:
                    return f
            elif (f.top_card.suit == card.suit and
                  f.top_card.rank == card.rank - 1):
                return f
        return None

    def tableau_accepts(self, card: Card) -> List[Pile]:
        """Return all tableau piles that can accept this card on top."""
        result = []
        for t in self.tableau:
            if t.is_empty:
                # Only kings can go on empty tableau
                if card.rank == Rank.KING:
                    result.append(t)
            elif (t.top_card and
                  not t.top_card.face_down and
                  t.top_card.color != card.color and
                  t.top_card.rank == card.rank + 1):
                result.append(t)
        return result

    def display(self) -> str:
        """Pretty-print the game state."""
        lines = []
        lines.append("=" * 60)

        # Stock and waste
        stock_str = f"[{len(self.stock.cards)}]" if self.stock.cards else "[_]"
        waste_str = str(self.waste.top_card) if self.waste.top_card else "[_]"
        waste_count = len(self.waste.cards)

        # Foundations
        found_strs = []
        for f in self.foundations:
            if f.top_card:
                found_strs.append(str(f.top_card))
            else:
                found_strs.append("[_]")

        lines.append(f"Stock: {stock_str} ({len(self.stock.cards)} cards)  "
                     f"Waste: {waste_str} ({waste_count} cards)  "
                     f"Foundations: {' '.join(found_strs)}")
        lines.append("-" * 60)

        # Tableau
        max_height = max((len(t.cards) for t in self.tableau), default=0)
        for row in range(max_height):
            row_strs = []
            for t in self.tableau:
                if row < len(t.cards):
                    row_strs.append(f"{str(t.cards[row]):>5}")
                else:
                    row_strs.append("     ")
            lines.append("  ".join(row_strs))

        if max_height == 0:
            lines.append("  (all tableau columns empty)")

        lines.append("=" * 60)
        return "\n".join(lines)
