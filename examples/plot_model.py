# -*- coding: utf-8 -*-
"""
=============================
Load and plot a model
=============================

Here we load the example model, and then plot it.

"""

# Code source: Andrew Heusser & Lucy Owen
# License: MIT

import superEEG as se

# load example model
model = se.load('example_model')

# plot it
# model.plot(xticklabels=False, yticklabels=False)
model.plot()