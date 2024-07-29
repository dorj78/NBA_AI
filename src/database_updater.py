"""
database_updater.py

Description:
This module handles the core process of updating the database with game data, including schedule updates, play-by-play logs,
game states, prior states, feature sets, and predictions.
It consists of functions to:
- Update the schedule for a given season.
- Update play-by-play logs and game states for games needing updates.
- Update prior states and feature sets for games with incomplete pre-game data.
- Update predictions for games needing updated predictions.

Functions:
    - update_database(season="Current", predictor=None, db_path=DB_PATH): Orchestrates the full update process for the specified season.
    - update_game_data(season, db_path=DB_PATH): Updates play-by-play logs and game states for games needing updates.
    - update_pre_game_data(season, db_path=DB_PATH): Updates prior states and feature sets for games with incomplete pre-game data.
    - update_prediction_data(season, predictor, db_path=DB_PATH): Generates and saves predictions for upcoming games.
    - get_games_needing_game_state_update(season, db_path=DB_PATH): Retrieves game_ids for games needing game state updates.
    - get_games_with_incomplete_pre_game_data(season, db_path=DB_PATH): Retrieves game_ids for games with incomplete pre-game data.
    - get_games_for_prediction_update(season, predictor, db_path=DB_PATH): Retrieves game_ids for games needing updated predictions.
    - main(): Main function to handle command-line arguments and orchestrate the update process.

Usage:
- Typically run as part of a larger data processing pipeline.
- Script can be run directly from the command line (project root) to update the database with the latest game data and predictions.
    python -m src.database_updater --log_level=DEBUG --season=2023-2024 --predictor=Random
- Successful execution will update the database with the latest game data and predictions for the specified season.
"""

import argparse
import sqlite3

from src.config import config
from src.features import create_feature_sets, load_feature_sets, save_feature_sets
from src.game_states import create_game_states, save_game_states
from src.logging_config import setup_logging
from src.pbp import get_pbp, save_pbp
from src.predictions import make_pre_game_predictions, save_predictions
from src.prior_states import determine_prior_states_needed, load_prior_states
from src.schedule import update_schedule
from src.utils import log_execution_time, lookup_basic_game_info

# Configuration
DB_PATH = config["database"]["path"]


@log_execution_time()
def update_database(season="Current", predictor=None, db_path=DB_PATH):
    """
    Orchestrates the full update process for the specified season.

    Parameters:
        season (str): The season to update (default is "Current").
        predictor: The prediction model to use (default is None).
        db_path (str): The path to the database (default is from config).

    Returns:
        None
    """
    # STEP 1: Update Schedule
    update_schedule(season)
    # STEP 2: Update Game Data (Play-by-Play Logs, Game States)
    update_game_data(season, db_path)
    # STEP 3: Update Pre Game Data (Prior States, Feature Sets)
    update_pre_game_data(season, db_path)
    # STEP 4: Update Predictions
    if predictor:
        update_prediction_data(season, predictor, db_path)


@log_execution_time()
def update_game_data(season, db_path=DB_PATH):
    """
    Updates play-by-play logs and game states for games needing updates.

    Parameters:
        season (str): The season to update.
        db_path (str): The path to the database (default is from config).

    Returns:
        None
    """
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
    """
    Updates prior states and feature sets for games with incomplete pre-game data.

    Parameters:
        season (str): The season to update.
        db_path (str): The path to the database (default is from config).

    Returns:
        None
    """
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
def update_prediction_data(season, predictor, db_path=DB_PATH):
    """
    Generates and saves predictions for upcoming games.

    Parameters:
        season (str): The season to update.
        predictor: The prediction model to use.
        db_path (str): The path to the database (default is from config).

    Returns:
        None
    """
    game_ids = get_games_for_prediction_update(season, predictor, db_path)
    feature_sets = load_feature_sets(game_ids, db_path)
    predictions = make_pre_game_predictions(feature_sets, predictor)
    save_predictions(predictions, predictor, db_path)


@log_execution_time()
def get_games_needing_game_state_update(season, db_path=DB_PATH):
    """
    Retrieves game_ids for games needing game state updates.

    Parameters:
        season (str): The season to filter games by.
        db_path (str): The path to the database (default is from config).

    Returns:
        list: A list of game_ids for games that need to be updated.
    """
    with sqlite3.connect(db_path) as db_connection:
        cursor = db_connection.cursor()
        # Query to identify games needing updates, including 'In Progress' games
        cursor.execute(
            """
            SELECT game_id 
            FROM Games 
            WHERE season = ?
              AND season_type IN ('Regular Season', 'Post Season') 
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
    Retrieves game_ids for games with incomplete pre-game data.

    Parameters:
        season (str): The season to filter games by.
        db_path (str): The path to the database (default is from config).

    Returns:
        list: A list of game_ids that need to have their pre_game_data_finalized flag updated.
    """
    query = """
    SELECT game_id
    FROM Games
    WHERE season = ?
      AND season_type IN ("Regular Season", "Post Season")
      AND pre_game_data_finalized = 0
      AND (status = 'Completed' OR status = 'In Progress')
    
    UNION

    SELECT g1.game_id
    FROM Games g1
    WHERE g1.season = ?
      AND g1.season_type IN ("Regular Season", "Post Season")
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
    Retrieves game_ids for games needing updated predictions.

    Parameters:
        season (str): The season to update.
        predictor (str): The predictor to check for existing predictions.
        db_path (str): The path to the database (default is from config).

    Returns:
        list: A list of game_ids that need updated predictions.
    """
    query = """
        SELECT g.game_id
        FROM Games g
        LEFT JOIN Predictions p ON g.game_id = p.game_id AND p.predictor = ?
        WHERE g.season = ?
            AND g.season_type IN ("Regular Season", "Post Season")
            AND g.pre_game_data_finalized = 1
            AND p.game_id IS NULL
        """

    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(query, (predictor, season))
        result = cursor.fetchall()

    game_ids = [row[0] for row in result]

    return game_ids


def main():
    """
    Main function to handle command-line arguments and orchestrate the update process.
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
