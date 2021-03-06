#!/bin/bash
#
# Train and evaluate lemmatizer. Run as:
#   ./run_lemma.sh TREEBANK OTHER_ARGS
# where TREEBANK is the UD treebank name (e.g., UD_English-EWT) and OTHER_ARGS are additional training arguments (see lemmatizer code) or empty.
# This script assumes UDBASE and LEMMA_DATA_DIR are correctly set in config.sh.

source scripts/config.sh

treebank=$1; shift
args=$@
short=`bash scripts/treebank_to_shorthand.sh ud $treebank`
lang=`echo $short | sed -e 's#_.*##g'`

eval_file=${LEMMA_DATA_DIR}/${short}.dev.in.conllu
output_file=${LEMMA_DATA_DIR}/${short}.dev.pred.conllu
gold_file=${LEMMA_DATA_DIR}/${short}.dev.gold.conllu

echo "Running lemmatizer with $args..."
python -m lexenlem.models.lemmatizer_cmb --data_dir $LEMMA_DATA_DIR --eval_file $eval_file \
        --output_file $output_file --gold_file $gold_file --lang $short --mode predict $args