from __future__ import annotations  # noqa: EXE002, D100

import json
import logging
from typing import TYPE_CHECKING

import pandas as pd
from transformers import (
    DataCollatorForSeq2Seq,
    RobertaTokenizerFast,
    Seq2SeqTrainer,
    Seq2SeqTrainingArguments,
    T5ForConditionalGeneration,
)

from src.data_prep_utils import prep_for_hf
from src.model import load_model_tok
from src.processing_utils import compute_metric_with_params, prepare_hg_ds

if TYPE_CHECKING:
    from datasets import Dataset

logging.basicConfig()
logging.getLogger().setLevel(logging.INFO)

EVAL_COLUMNS = [
    "eval_loss",
    "eval_rouge1",
    "eval_rouge2",
    "eval_rougeL",
    "eval_rougeLsum",
    "eval_bleu",
    "eval_gen_len",
]
LOGS_COLUMNS = [
    "epoch",
    "loss",
    "step",
    "eval_loss",
    "eval_rouge1",
    "eval_rouge2",
    "eval_rougeL",
    "eval_rougeLsum",
    "eval_bleu",
]

def prepare_trainer(
        train_data: Dataset,
        validation_data: Dataset,
        training_arguments:dict,
        model: T5ForConditionalGeneration | str,
        tokenizer: RobertaTokenizerFast,
        ) -> list:
    """Prepare HuggingFace trainer."""
    analysis_name = f'{training_arguments["MODEL"]}_{training_arguments["TRAIN_N"]}_{training_arguments["BATCH_SIZE"]}'
    training_args = Seq2SeqTrainingArguments(
        **training_arguments["SEQ_TRAINER_ARGS"],
    )

    data_collator = DataCollatorForSeq2Seq(tokenizer, model=model)
    compute_metrics = compute_metric_with_params(tokenizer)

    trainer = Seq2SeqTrainer(
        model=model,
        args=training_args,
        data_collator=data_collator,
        train_dataset=train_data,
        eval_dataset=validation_data,
        tokenizer=tokenizer,
        compute_metrics=compute_metrics,
    )
    return trainer, analysis_name

def train(
        train_data: Dataset,
        validation_data: Dataset,
        training_arguments: dict,
        model: T5ForConditionalGeneration,
        tokenizer: RobertaTokenizerFast,
        ) -> Seq2SeqTrainer:
    """Train the T5 Model."""
    trainer, analysis_name = prepare_trainer(train_data, validation_data, training_arguments, model, tokenizer)
    # TRAINING
    trainer.train()
    trainer.save_model(f"reports/{analysis_name}/trained_model")
    return trainer

def dd_eval(  # noqa: PLR0913
        train_data: Dataset,
        validation_data: Dataset,
        training_arguments: dict,
        model: T5ForConditionalGeneration | str,
        tokenizer: RobertaTokenizerFast,
        trainer: Seq2SeqTrainer=None,
        ) -> list:
    """Evaluate HF on validation data."""
    analysis_name = f'{training_arguments["MODEL"]}_{training_arguments["TRAIN_N"]}_{training_arguments["BATCH_SIZE"]}'
    if isinstance(model, str):
        model = load_model_tok(f"reports/{analysis_name}/trained_model", freeze=True)
    trainer, analysis_name = prepare_trainer(train_data, validation_data, training_arguments, model, tokenizer)
    # ZERO - SHOT
    results_zero_shot = trainer.evaluate()
    results_zero_shot_df = pd.DataFrame(data=results_zero_shot, index=[0])[EVAL_COLUMNS]
    results_zero_shot_df.loc[0, :] = results_zero_shot_df.loc[0, :].apply(lambda x: round(x, 3))
    results_zero_shot_df.to_csv(f"reports/{analysis_name}/zero_shot_results.csv", index=False)
    logging.info(results_zero_shot_df)

    # STORE PARAMS
    with open(f"reports/{analysis_name}/config.json", "w") as params_file:  # noqa: PTH123
        json.dump(training_arguments, params_file)

    return trainer, {"loss" : results_zero_shot_df["eval_loss"], "rouge" : results_zero_shot_df["eval_rouge1"]}, analysis_name

def parse_logs(trainer: Seq2SeqTrainer) -> pd.DataFrame:
    """Parse Trainer Logs."""
    log_history = trainer.state.log_history
    train_log = pd.DataFrame(columns=log_history[0].keys())
    eval_log = pd.DataFrame(columns=log_history[1].keys())

    for log in log_history:
        if "loss" in log:
            train_log = pd.concat(
                [train_log, pd.DataFrame.from_dict(log, orient="index").T],
                axis=0,
            )
        elif "eval_loss" in log:
            eval_log = pd.concat(
                [eval_log, pd.DataFrame.from_dict(log, orient="index").T],
                axis=0,
            )

    logs = train_log.merge(
        eval_log,
        how="inner",
        left_on=["epoch", "step"],
        right_on=["epoch", "step"],
    )
    return logs[LOGS_COLUMNS]

def dd_analysis(
        df:pd.DataFrame,
        tokenizer:RobertaTokenizerFast,
        model: T5ForConditionalGeneration,
        training_args: dict,
        ) -> pd.DataFrame:
    """Runs analysis on data drift along time dimension."""  # noqa: D401
    loss = []
    rouge_1 = []

    train_dataset = prep_for_hf(df, -1)
    train_data = prepare_hg_ds(
        train_dataset,
        tokenizer=tokenizer,
        max_input_length=training_args["ENCODER_LENGTH"],
        max_output_length=training_args["DECODER_LENGTH"],
    )

    for i in range(df.t_batch.nunique()-1):
        logging.info(f"Validation for time step {i}")  # noqa: G004
        test_dataset  = prep_for_hf(df, i)
        validation_data = prepare_hg_ds(
            test_dataset,
            tokenizer=tokenizer,
            max_input_length=training_args["ENCODER_LENGTH"],
            max_output_length=training_args["DECODER_LENGTH"],
        )
        if i==0:
            trainer = train(train_data, validation_data, training_args, model, tokenizer)
        trainer, results, analysis_name = dd_eval(train_data, validation_data, training_args, model, tokenizer)
        loss.append(results["loss"].iloc[0])
        rouge_1.append(results["rouge"].iloc[0])
    loss_df = pd.DataFrame(data={"i": range(df.t_batch.nunique()-1),
                    "loss" : loss,
                    "rouge": rouge_1})
    loss_df.to_csv(f"reports/{analysis_name}/ts_logs.csv", index=False)
    return loss_df
