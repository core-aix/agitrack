def print_board(board):
    for i, row in enumerate(board):
        print(" | ".join(cell if cell else " " for cell in row))
        if i < len(board) - 1:
            print("--+---+--")


def check_rows(board):
    for row in board:
        if row[0] and len(set(row)) == 1:
            return row[0]
    return None


def check_columns(board):
    for col in range(3):
        if board[0][col] and all(board[r][col] == board[0][col] for r in range(3)):
            return board[0][col]
    return None


def check_diagonals(board):
    if board[0][0] and board[0][0] == board[1][1] == board[2][2]:
        return board[0][0]
    if board[0][2] and board[0][2] == board[1][1] == board[2][0]:
        return board[0][2]
    return None


def check_winner(board):
    return check_rows(board) or check_columns(board) or check_diagonals(board)


def is_draw(board):
    return check_winner(board) is None and is_board_full(board)


def is_board_full(board):
    return all(cell for row in board for cell in row)


def current_player(turn):
    return "X" if turn % 2 == 0 else "O"


def validate_move(board, row, col):
    if not (0 <= row < 3 and 0 <= col < 3):
        return False, "Position out of range (0-2)."
    if board[row][col]:
        return False, "That cell is already taken."
    return True, ""


def get_player_input(player):
    move = input(f"{player}'s turn (row,col): ")
    parts = move.split(",")
    if len(parts) != 2:
        raise ValueError
    row, col = int(parts[0].strip()), int(parts[1].strip())
    return row, col


def make_move(board, row, col, player):
    board[row][col] = player
    return board


def main():
    board = [["" for _ in range(3)] for _ in range(3)]
    turn = 0

    while True:
        print_board(board)
        player = current_player(turn)

        winner = check_winner(board)
        if winner:
            print(f"\n{winner} wins!")
            break

        if is_draw(board):
            print_board(board)
            print("\nDraw!")
            break

        try:
            row, col = get_player_input(player)
            valid, msg = validate_move(board, row, col)
            if valid:
                make_move(board, row, col, player)
                turn += 1
            else:
                print(msg)
        except (ValueError, IndexError):
            print("Enter row,col as two numbers (0-2).")


if __name__ == "__main__":
    main()
