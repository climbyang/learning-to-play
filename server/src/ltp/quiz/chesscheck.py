"""Machine check for chess templates: the keyed move must be the only option that mates.

Used by the production bank test and by question generation. Needs python-chess.
"""
from __future__ import annotations

import json

from ltp.quiz.bank import Instance

CHESS_KINDS = ("chess_mate1", "chess_mate2")


def _mates_in_one(board) -> bool:
    for move in list(board.legal_moves):
        board.push(move)
        mate = board.is_checkmate()
        board.pop()
        if mate:
            return True
    return False


def _forces_mate_in_two(board, move) -> bool:
    board.push(move)
    try:
        if board.is_checkmate():
            return False  # a mate in one is not a mate-in-two answer
        replies = list(board.legal_moves)
        if not replies:
            return False  # stalemate
        for reply in replies:
            board.push(reply)
            ok = _mates_in_one(board)
            board.pop()
            if not ok:
                return False
        return True
    finally:
        board.pop()


def fen_problem(fen: object) -> str | None:
    import chess
    try:
        ok = isinstance(fen, str) and chess.Board(fen).is_valid()
    except ValueError:
        ok = False
    return None if ok else "chess needs a valid fen"


def chess_problem(i: Instance) -> str | None:
    """Why a chess instance's keyed answer is not the one winning option, or None."""
    import chess
    try:
        fen = json.loads(i.figure or "")["fen"]
    except (ValueError, KeyError, TypeError):
        return "chess figure needs a fen"
    if (why := fen_problem(fen)) is not None:
        return why
    board = chess.Board(fen)
    moves = {}
    for oid, text in i.options:
        if "#" in text or "+" in text:
            return f"option {text!r} gives the answer away"
        try:
            moves[oid] = board.parse_san(text)
        except ValueError:
            return f"option {text!r} is not a legal move"
    if len(set(moves.values())) != len(moves):
        return "duplicate moves among options"
    if i.kind == "chess_mate1":
        def wins(m) -> bool:
            board.push(m)
            mate = board.is_checkmate()
            board.pop()
            return mate
    elif i.kind == "chess_mate2":
        if _mates_in_one(board):
            return "position has a mate in one"

        def wins(m) -> bool:
            return _forces_mate_in_two(board, m)
    else:
        return f"unknown chess kind {i.kind!r}"
    winners = {oid for oid, m in moves.items() if wins(m)}
    return None if winners == {i.answer} else f"winning options {sorted(winners)}, keyed {i.answer!r}"
