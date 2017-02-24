#!/usr/bin/env python3
# -*- coding: UTF-8 -*-

"""
Evaluates the performance of the fixer thing.
"""

import csv
import tempfile
import math
import sys
from pathlib import Path

from tqdm import tqdm

from vocabulary import vocabulary
from mutate import Persistence as Mutations
from mutate import SourceCode
from corpus import Corpus
from condensed_corpus import CondensedCorpus
from training_utils import Sentences, one_hot_batch
from detect import (
    check_syntax_file, tokenize_file, chop_prefix,
    harmonic_mean, index_of_max, rank,
    Fixes, Agreement, id_to_token
)
from vectorize_tokens import vectorize_tokens
from model_recipe import ModelRecipe


def fix_zeros(preds, epsilon=sys.float_info.epsilon):
    """
    Replace zeros with really small values..
    """
    for i, pred in enumerate(preds):
        if math.isclose(pred, 0.0):
            preds[i] = epsilon


# TODO: can create tests to check the value of agreement...

def harmonic_mean_agreement(prefix_pred, suffix_pred):
    # Avoid NaNs
    fix_zeros(prefix_pred)
    fix_zeros(suffix_pred)

    mean = harmonic_mean(prefix_pred, suffix_pred)
    # Rank the values from lowest to top (???)
    paired_rankings = rank(mean)
    # Get the value with the minimum probability (???)
    _, min_prob = paired_rankings[0]

    return min_prob


def squared_error_agreement(prefix_pred, suffix_pred):
    """
    Return the agreement (probability) of this token using sum of squared
    errors.
    """
    # Pretend the sum of squared error is like the cross-entropy of
    # prefix and suffix.
    entropy = ((prefix_pred - suffix_pred) ** 2).sum()
    return -entropy


class SensibilityForEvaluation:
    sentence_length = 20

    def __init__(self, fold_no):
        name = 'javascript-{dir}-300-20.{fold}.5.h5'
        forwards = ModelRecipe.from_string(name.format(dir='f', fold=fold_no))
        backwards = ModelRecipe.from_string(name.format(dir='b', fold=fold_no))
        db = mutations
        self.forwards_predict = lambda prefix: db.get_prediction(model_recipe=forwards, context=prefix)
        self.backwards_predict = lambda suffix: db.get_prediction(model_recipe=backwards, context=suffix)

    def rank_and_fix(self, filename):
        """
        Rank the syntax error location (in token number) and returns a possible
        fix for the given filename.
        """

        # Get file vector for the incorrect file.
        with open(str(filename), 'rt', encoding='UTF-8') as script:
            # NOTE! INDICIES IN tokens ARE OFFSET BY ONE (- 1 from other
            # mentions of "index")
            tokens = tokenize_file(script)
        file_vector = vectorize_tokens(tokens)

        padding = self.sentence_length

        # Holds the lowest agreement at each point in the file.
        least_agreements = []

        # These will hold the TOP predictions at a given point.
        forwards_predictions = [None] * padding
        backwards_predictions = [None] * padding
        contexts = enumerate(self.contexts(file_vector), start=padding)

        for index, ((prefix, token), (suffix, _)) in contexts:
            assert token == file_vector[index], (
                str(token) + ' ' + str(file_vector[index])
            )

            # Fetch predictions.
            prefix_pred = self.forwards_predict(prefix)
            suffix_pred = self.backwards_predict(suffix)

            assert math.isclose(sum(prefix_pred), 1.0, rel_tol=0.01)
            assert math.isclose(sum(suffix_pred), 1.0, rel_tol=0.01)

            # Store the TOP prediction from both models.
            top_next_prediction = index_of_max(prefix_pred)
            forwards_predictions.append(top_next_prediction)
            top_prev_prediction = index_of_max(suffix_pred)
            backwards_predictions.append(top_prev_prediction)
            assert top_next_prediction == forwards_predictions[index]

            agreement = Agreement(
                squared_error_agreement(prefix_pred, suffix_pred),
                index
            )
            print("%4d  %.3f %5.2f%% ::: (%s)>>> %s <<<(%s)" % (
                index,
                agreement.probability,
                harmonic_mean_agreement(prefix_pred, suffix_pred) * 100,
                vocabulary.to_text(top_next_prediction),
                tokens[index - 1],
                vocabulary.to_text(top_prev_prediction),
            ))
            least_agreements.append(agreement)

        fixes = Fixes(tokens, offset=1)

        # For the top disagreements, synthesize fixes.
        least_agreements.sort()
        for disagreement in least_agreements[:3]:
            print(disagreement)
            pos = disagreement.index

            # Assume an addition. Let's try removing some tokens.
            fixes.try_remove(pos)

            # Assume a deletion. Let's try inserting some tokens.
            fixes.try_insert(pos, id_to_token(forwards_predictions[pos]))
            fixes.try_insert(pos, id_to_token(backwards_predictions[pos]))
            # TODO: make substitution rule

        fix = None if not fixes else tuple(fixes)[0]

        # TODO: I might need "fix position" as well as fix.
        return least_agreements, fix

    def contexts(self, file_vector):
        """
        Yield every context (prefix, suffix) in the given file vector.
        """
        sent_forwards = Sentences(file_vector,
                                  size=self.sentence_length,
                                  backwards=False)
        sent_backwards = Sentences(file_vector,
                                   size=self.sentence_length,
                                   backwards=True)
        return zip(sent_forwards, chop_prefix(sent_backwards))

    @staticmethod
    def is_okay(filename):
        """
        Check if the syntax is okay.
        """
        with open(filename, 'rb') as source_file:
            return check_syntax_file(source_file)


class Results:
    FIELDS = '''
        fold file
        mkind mpos mtoken
        correct_line line_of_top_rank rank_correct_line
        fixed fkind fpos ftoken same_fix
    '''.split()

    def __enter__(self):
        self._file = open('results.csv', 'w')
        self._writer = csv.DictWriter(self._file, fieldnames=self.FIELDS)
        self._writer.writeheader()
        return self

    def __exit__(self, *exc_info):
        self._file.close()

    def write(self, **kwargs):
        self._writer.writerow(kwargs)
        self._file.flush()


FOLDS = {}

def populate_folds():
    """
    Create a mapping between a file hash and the fold it came from.
    """
    for fold in 0, 1, 2, 3, 4:
        with open('test_set_hashes.' + str(fold)) as hash_file:
            for file_hash in hash_file:
                FOLDS[file_hash.strip()] = fold


def apply_mutation(mutation, program):
    mutated_file = tempfile.NamedTemporaryFile(mode='w+t', encoding='UTF-8')
    # Apply the mutatation and write it to disk.
    mutation.format(program, mutated_file)
    mutated_file.flush()
    return mutated_file


def rank_and_fix(fold_no, mutated_file):
    sensibility = SensibilityForEvaluation(fold_no)
    return sensibility.rank_and_fix(mutated_file.name)


def first_with_line_no(disagreements, correct_line, tokens):
    for rank, disagreement in enumerate(disagreements, start=1):
        if tokens[disagreement.index].line == correct_line:
            return rank


def location_of_vectors():
    shared_memory = Path('/dev/shm/javascript.sqlite3')
    current_dir = Path('./javascript.sqlite3')
    return str(
        shared_memory if shared_memory.exists() else current_dir
    )


if __name__ == '__main__':
    corpus = Corpus.connect_to('javascript-sources.sqlite3')
    vectors = CondensedCorpus.connect_to(location_of_vectors())
    populate_folds()

    with Mutations() as mutations, Results() as results:
        for file_hash, mutation in tqdm(mutations):
            # Figure out what fold it's in.
            fold_no = FOLDS[file_hash]

            # Get the original vector to get the mutated file.
            _, vector = vectors[file_hash]
            assert vector[0] == 0, 'not start token'
            assert vector[-1] == 99, 'not end token'
            program = SourceCode(file_hash, vector)

            # Get the actual file's tokens, including line numbers!
            tokens = corpus.get_tokens(file_hash)
            # Ensure that both files use the same indices!
            tokens = ('start',) + tokens + ('end',)
            assert len(tokens) == len(tokens)

            # Figure out the line of the mutation in the original file.
            correct_line = tokens[mutation.location].line

            # Apply the original mutation.
            with apply_mutation(mutation, program) as mutated_file:
                print(mutation, vocabulary.to_text(mutation.token) if mutation.token else '')
                # Do the (canned) prediction...
                ranked_locations, fix = rank_and_fix(fold_no, mutated_file)

            # Figure out the rank of the actual mutation.
            line_of_top_location = tokens[ranked_locations[0].index].line
            rank_correct_line = first_with_line_no(ranked_locations,
                                                   correct_line, tokens)

            results.write(
                fold=fold_no,
                file=file_hash,
                mkind=mutation.name,
                mtoken=mutation.token,
                mpos=mutation.location,
                correct_line=correct_line,
                line_of_top_rank=line_of_top_location,
                rank_correct_line=rank_correct_line,
                fixed=bool(fix),
                fkind=fix.kind if fix else None,
                fpos=fix.location if fix else None,
                ftoken=fix.token if fix else None,
                same_fix=None
            )
