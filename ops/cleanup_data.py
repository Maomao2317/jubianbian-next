#!/usr/bin/env python3
"""Compatibility entry point for the existing weekly maintenance job.

The job now removes only expired video media. Task rows and screenplay results
are retained indefinitely.
"""
from video_retention import main


if __name__ == "__main__":
    main()
