"""
Utils and wrappers for scoring lemmatizers.
"""
from lexenlem.utils import conll18_ud_eval as ud_eval


def score(system_conllu_file, gold_conllu_file):
    """ Wrapper for lemma scorer. """
    gold_ud = ud_eval.load_conllu_file(gold_conllu_file)
    system_ud = ud_eval.load_conllu_file(system_conllu_file)
    evaluation = ud_eval.evaluate(gold_ud, system_ud)
    el = evaluation["Lemmas"]
    precision, recall, f1 = el.precision, el.recall, el.f1
    return precision, recall, f1
