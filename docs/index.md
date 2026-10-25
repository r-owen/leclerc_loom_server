# Leclerc Web Server

Software that allows you to control a Leclerc dobby loom from any modern web browser, e.g. on a phone or tablet.

For more information, read the [main documentation](https://r-owen.github.io/base_loom_server/).

Caveats:

* This software has not yet been tested on a real loom and one important question about a status bit remains unanswered so I had to guess.
* Leclerc looms do not fully protect themselves from non-optimal timing for energizing and de-energizing the solenoids. If this software does not appear to be operating correctly please stop using it and, if you are willing, report a bug.

## Links

* leclerc_loom_server depends on [base_loom_server](https://r-owen.github.io/base_loom_server/), which does most of the work.
* leclerc_loom_server is served on [PyPI](https://pypi.org/project/leclerc-loom-server/).
* Source code is on [github](https://r-owen.github.io/leclerc_loom_server/).
* Please report issues using base_loom_server's [issue tracker](https://github.com/r-owen/base_loom_server/issues).
* Help with [translations to other languages](https://r-owen.github.io/base_loom_server/translations) is much appreciated. 

## Acknowledgements

Thanks to Francois Brassard from Leclerc for providing the API and helpful clarification.
