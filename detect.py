#!/usr/bin/env python
# -*- coding: UTF-8 -*-

# Copyright 2016 Eddie Antonio Santos <easantos@ualberta.ca>
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import io
import json
import subprocess
from pathlib import Path

import numpy as np
from keras.models import model_from_json
from blessings import Terminal

from unvocabularize import unvocabularize
from vectorize_tokens import vectorize_tokens
from corpus import Token
from vocabulary import vocabulary
from training_utils import Sentences, one_hot_batch


THIS_DIRECTORY = Path(__file__).parent
TOKENIZE_JS_BIN = ('node', str(THIS_DIRECTORY / 'tokenize-js'))

parser = argparse.ArgumentParser()
parser.add_argument('filename', nargs='?', type=Path,
                    default=Path('/dev/stdin'))
parser.add_argument('--architecture', type=Path,
                    default=THIS_DIRECTORY / 'model-architecture.json')
parser.add_argument('--weights-forwards', type=Path,
                    default=THIS_DIRECTORY / 'javascript-tiny.5.h5')


def tokenize_file(file_obj):
    """
    >>> import tempfile
    >>> with tempfile.TemporaryFile('w+t', encoding='utf-8') as f:
    ...     f.write('$("hello");')
    ...     f.seek(0)
    ...     tokens = tokenize_file(f)
    11
    0
    >>> len(tokens)
    5
    >>> isinstance(tokens[0], Token)
    True
    """
    status = subprocess.run(TOKENIZE_JS_BIN,
                           check=True,
                           stdin=file_obj,
                           stdout=subprocess.PIPE)
    return [
        Token.from_json(raw_token)
        for raw_token in json.loads(status.stdout.decode('UTF-8'))
    ]


class Model:
    """
    >>> model = Model.from_filenames(architecture='model-architecture.json',
    ...                              weights='javascript-tiny.5.h5')
    >>> comma = vocabulary.to_index(',')
    >>> answer = model.predict([comma] * 19)
    >>> len(answer) == len(vocabulary)
    True
    >>> answer[comma] > 0.5
    True
    """
    def __init__(self, model):
        self.model = model

    def predict(self, vector):
        """
        TODO: Create predict() for entire file as a batch?
        """
        x, y = one_hot_batch([(vector, 0)], batch_size=1, sentence_length=20)
        return self.model.predict(x, batch_size=1)[0]

    @classmethod
    def from_filenames(cls, *, architecture=None, weights=None):
        with open(architecture) as archfile:
            model = model_from_json(archfile.read())
        model.load_weights(weights)

        return cls(model)


def rank(predictions):
    return list(sorted(enumerate(predictions),
                       key=lambda t: t[1], reverse=True))

if __name__ == '__main__':
    globals().update(vars(parser.parse_args()))

    assert architecture.exists()
    assert weights_forwards.exists()

    with open(str(filename), 'rt', encoding='UTF-8') as script:
        tokens = tokenize_file(script)

    file_vector = vectorize_tokens(tokens)
    forwards = Model.from_filenames(architecture=str(architecture),
                                    weights=str(weights_forwards))

    t = Terminal()

    for sentence, actual in Sentences(file_vector, size=20):
        predictions = forwards.predict(sentence)
        as_text = unvocabularize(sentence)
        found_it = False

        print("For {t.underline}{as_text}{t.normal}, "
              "got:".format(t=t, as_text=as_text))
        for token_id, weight in rank(predictions)[:5]:
            color = t.green if token_id == actual else ''
            if token_id == actual:
                found_it = True
            text = vocabulary.to_text(token_id)
            prob = weight * 100.0
            print("   {prob:5.2f}% → "
                  "{color}{text}{t.normal}".format(text=text,
                                                   prob=prob,
                                                   color=color,
                                                   t=t))

        if not found_it:
            print("{t.red}Actual{t.normal}: {t.underline}{actual}{t.normal}"
                  "".format(t=t, actual=vocabulary.to_text(actual)))
        print()
