import argparse
import logging
import sqlite3

from src.config import config
from src.features import create_feature_sets, load_feature_sets, save_feature_sets
from src.game_states import create_game_states, save_game_states
from src.logging_config import setup_logging
from src.pbp import get_pbp, save_pbp
from src.predictions import make_predictions, save_predictions
from src.prior_states import determine_prior_states_needed, load_prior_states
from src.schedule import update_schedule
from src.utils import log_execution_time, lookup_basic_game_info

# Configuration
DB_PATH = config["database"]["path"]


def update_database(season="Current", predictor=None, db_path=DB_PATH):
    # STEP 1: Update Schedule
    update_schedule(season)
    # STEP 2: Update Game Data (Play-by-Play Logs, Game States)
    update_game_data(season, db_path)
    # STEP 3: Update Pre Game Data (Prior States, Feature Sets)
    update_pre_game_data(season, db_path)
    # STEP 4: Update Predictions
    if predictor:
        update_predictions(season, predictor, db_path)


@log_execution_time()
def update_game_data(season, db_path=DB_PATH):
    game_ids = get_games_needing_game_state_update(season, db_path)
    basic_game_info = lookup_basic_game_info(game_ids, db_path)
    pbp_data = get_pbp(game_ids)
    save_pbp(pbp_data, db_path)

    game_state_inputs = {}
    for game_id, game_info in pbp_data.items():
        game_state_inputs[game_id] = {
            "home": basic_game_info[game_id]["home"],
            "away": basic_game_info[game_id]["away"],
            "date_time_est": basic_game_info[game_id]["date_time_est"],
            "pbp_logs": game_info,
        }

    game_states = create_game_states(game_state_inputs)
    save_game_states(game_states)


@log_execution_time()
def update_pre_game_data(season, db_path=DB_PATH):
    game_ids = get_games_with_incomplete_pre_game_data(season, db_path)
    prior_states_needed = determine_prior_states_needed(game_ids, db_path)
    prior_states_dict = load_prior_states(prior_states_needed, db_path)
    feature_sets = create_feature_sets(prior_states_dict, db_path)
    save_feature_sets(feature_sets, db_path)

    # Categorize games and prepare data for database update
    games_update_data = []
    for game_id, states in prior_states_dict.items():
        pre_game_data_finalized = int(
            not states["missing_prior_states"]["home"]
            and not states["missing_prior_states"]["away"]
        )
        games_update_data.append((pre_game_data_finalized, game_id))

    # Update database in a single transaction
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.executemany(
            """
            UPDATE Games
            SET pre_game_data_finalized = ?
            WHERE game_id = ?
        """,
            games_update_data,
        )
        conn.commit()


@log_execution_time()
def update_predictions(season, predictor, db_path=DB_PATH):
    game_ids = get_games_for_prediction_update(season, predictor, db_path)
    feature_sets = load_feature_sets(game_ids, db_path)
    predictions = make_predictions(feature_sets, predictor)
    save_predictions(predictions, predictor, db_path)


@log_execution_time()
def get_games_needing_game_state_update(season, db_path=DB_PATH):
    """
    Identify games that need to have their data updated for a given season.

    This function filters games by the specified season and checks for
    completed games that have not yet been marked as fully updated in terms
    of play-by-play logs and game states.

    Args:
        season: The season to filter games by (e.g., '2023-2024').
        db_path: The file path to the SQLite database. Default to DB_PATH from config.

    Returns:
        A list of game_ids for games that need to be updated.
    """
    with sqlite3.connect(db_path) as db_connection:
        cursor = db_connection.cursor()
        # Query to identify games needing updates, including 'In Progress' games
        cursor.execute(
            """
            SELECT game_id 
            FROM Games 
            WHERE season = ? 
              AND (status = 'Completed' OR status = 'In Progress')
              AND game_data_finalized = False;
        """,
            (season,),
        )

        games_to_update = cursor.fetchall()

    # Return a list of game_ids
    return [game_id for (game_id,) in games_to_update]


@log_execution_time()
def get_games_with_incomplete_pre_game_data(season, db_path=DB_PATH):
    """
    Retrieves game_ids for games that should have their pre_game_data_finalized flag set to True but do not.

    The function checks:
    1. Games in the specified season with status 'Completed' or 'In Progress' and pre_game_data_finalized = 0.
    2. Games in the specified season with status 'Not Started' where all prior games involving the teams
       (home team or away team) have game_data_finalized = True.

    Parameters:
    season (str): The season to filter games by.
    db_path (str): Path to the SQLite database file.

    Returns:
    list: List of game_ids that need to have their pre_game_data_finalized flag updated.
    """

    query = """
    SELECT game_id
    FROM Games
    WHERE season = ?
      AND pre_game_data_finalized = 0
      AND (status = 'Completed' OR status = 'In Progress')
    
    UNION

    SELECT g1.game_id
    FROM Games g1
    WHERE g1.season = ?
      AND g1.pre_game_data_finalized = 0
      AND g1.status = 'Not Started'
      AND NOT EXISTS (
          SELECT 1
          FROM Games g2
          WHERE g2.season = ?
            AND g2.date_time_est < g1.date_time_est
            AND (g2.home_team = g1.home_team OR g2.away_team = g1.home_team OR g2.home_team = g1.away_team OR g2.away_team = g1.away_team)
            AND g2.game_data_finalized = 0
      )
    """

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(query, (season, season, season))
        results = cursor.fetchall()

    return [row[0] for row in results]


@log_execution_time()
def get_games_for_prediction_update(season, predictor, db_path=DB_PATH):
    """
    Get game IDs that need updated predictions for a given season and predictor.

    Parameters:
    season (str): The season to filter games by.
    predictor (str): The predictor to check for existing predictions.
    db_path (str): The path to the SQLite database file.

    Returns:
    list: A list of game_ids that need updated predictions.
    """
    query = """
        SELECT g.game_id
        FROM Games g
        LEFT JOIN Predictions p ON g.game_id = p.game_id AND p.predictor = ?
        WHERE g.season = ?
            AND g.pre_game_data_finalized = 1
            AND p.game_id IS NULL
        """

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(query, (predictor, season))
        result = cursor.fetchall()

    return result


def main():
    """
    Main function to update the database with the latest game data and predictions.
    """
    parser = argparse.ArgumentParser(
        description="Update the database with the latest game data and predictions."
    )
    parser.add_argument(
        "--log_level",
        type=str,
        default="INFO",
        help="The logging level. Default is INFO. DEBUG provides more details.",
    )
    parser.add_argument(
        "--season",
        default="Current",
        type=str,
        help="The season to update. Default is 'Current'.",
    )
    parser.add_argument(
        "--predictor",
        default=None,
        type=str,
        help="The predictor to use for predictions.",
    )

    args = parser.parse_args()
    log_level = args.log_level.upper()
    setup_logging(log_level=log_level)

    update_database(args.season, args.predictor)


if __name__ == "__main__":
    main()
