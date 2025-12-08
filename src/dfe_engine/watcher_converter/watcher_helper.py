import pandas as pd
import numpy as np

# Load the Utility Functions
continous_summary_cols = [
    "Feature",
    "missing_values",
    "missing_percentage",
    "cardinality",
    "minimum",
    "25th Percentile",
    "mean",
    "median",
    "75th Percentile",
    "maximum",
    "IQR",
    "stand_dev",
]

categorical_summary_cols = [
    "Feature",
    "missing_values",
    "missing_percentage",
    "cardinality",
    "first_mode",
    "first_mode_count",
    "first_mode_percentage",
    "second_mode",
    "second_mode_count",
    "second_mode_percentage",
]


def generate_con_abt(data_frame, continuous_features):
    con_summary_stats_list = []
    for feature in continuous_features:
        populated_values = data_frame[feature].count()
        missing_values = data_frame[feature].isnull().sum()
        missing_percentage = (
            100 if populated_values == 0 else ((missing_values / data_frame[feature].size) * 100)
        )

        cardinality = len(data_frame[feature].unique())
        minimum = data_frame[feature].min(skipna=True)
        percentile_25 = data_frame[feature].quantile(0.25)
        mean = data_frame[feature].mean(skipna=True)
        median = data_frame[feature].median(skipna=True)
        percentile_75 = data_frame[feature].quantile(0.75)
        maximum = data_frame[feature].max()
        IQR = percentile_75 - percentile_25
        stand_dev = data_frame[feature].std(skipna=True)

        line_stats_list = [
            feature,
            missing_values,
            missing_percentage,
            cardinality,
            minimum,
            percentile_25,
            mean,
            median,
            percentile_75,
            maximum,
            IQR,
            stand_dev,
        ]
        con_summary_stats_list.append(line_stats_list)

    return pd.DataFrame.from_records(con_summary_stats_list, columns=continous_summary_cols)


def generate_cat_abt(data_frame, categorical_features):
    cat_summary_stats_list = []
    for feature in categorical_features:
        populated_values = data_frame[feature].count()
        missing_values = data_frame[feature].isnull().sum()
        missing_percentage = (
            100 if populated_values == 0 else ((missing_values / data_frame[feature].size) * 100)
        )

        if data_frame[feature].apply(isinstance, args=(list,)).any():  # check if any row is a list
            flattened_list = [
                item for sublist in data_frame[feature] for item in sublist
            ]  # flatten the list
            data_frame[feature] = pd.Series(
                flattened_list
            )  # replace the column with flattened list
        elif (
            data_frame[feature].apply(isinstance, args=(np.ndarray,)).any()
        ):  # check if any row is a numpy array
            flattened_list = [
                item for sublist in data_frame[feature] for item in sublist
            ]  # flatten the array
            data_frame[feature] = pd.Series(
                flattened_list
            )  # replace the column with flattened list

        cardinality = len(data_frame[feature].unique())
        first_mode = "NONE"
        first_mode_count = "NONE"
        first_mode_percentage = "NONE"
        second_mode = "NONE"
        second_mode_count = "NONE"
        second_mode_percentage = "NONE"

        mode_df = data_frame[feature].value_counts()

        if mode_df.size > 0:
            first_mode = mode_df.keys()[0]
            first_mode_count = mode_df[0]
            first_mode_percentage = (first_mode_count / populated_values) * 100

        if mode_df.size > 1:
            second_mode = mode_df.keys()[1]
            second_mode_count = mode_df[1]
            second_mode_percentage = (second_mode_count / populated_values) * 100

        line_stats_list = [
            feature,
            missing_values,
            missing_percentage,
            cardinality,
            first_mode,
            first_mode_count,
            first_mode_percentage,
            second_mode,
            second_mode_count,
            second_mode_percentage,
        ]
        cat_summary_stats_list.append(line_stats_list)

    return pd.DataFrame.from_records(cat_summary_stats_list, columns=categorical_summary_cols)
