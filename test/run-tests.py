#!/usr/bin/env python
# Copyright 2015 The Native Client Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import argparse
import difflib
import fnmatch
import logging
import os
import re
import shlex
import subprocess
import sys
import time


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT_DIR = os.path.dirname(SCRIPT_DIR)
DEFAULT_PNACL_EXE = os.path.join(REPO_ROOT_DIR, 'out', 'pnacl-opt-assert')
logger = logging.getLogger(__name__)


class Error(Exception):
  pass


def AsList(value):
  if value is None:
    return []
  elif type(value) is list:
    return value
  else:
    return [value]


def Indent(s, spaces):
  return ''.join(' '*spaces + l for l in s.splitlines(1))


def DiffLines(expected, actual):
  expected_lines = expected.splitlines(1)
  actual_lines = actual.splitlines(1)
  return list(difflib.unified_diff(expected_lines, actual_lines,
                                   fromfile='expected', tofile='actual'))


def FindTestFiles(directory, ext, filter_pattern_re):
  tests = []
  for root, dirs, files in os.walk(directory):
    for f in files:
      path = os.path.join(root, f)
      if os.path.splitext(f)[1] == ext:
        tests.append(os.path.relpath(path, SCRIPT_DIR))
  tests.sort()
  return [test for test in tests if re.match(filter_pattern_re, test)]


class TestInfo(object):
  def __init__(self):
    self.name = ''
    self.header_lines = []
    self.stdout_file = None
    self.expected_stdout = ''
    self.expected_stderr = ''
    self.exe = ''
    self.pexe = ''
    self.flags = []
    self.args = []
    self.expected_error = 0
    self.slow = False

  def Parse(self, filename):
    self.name = filename

    with open(filename) as f:
      seen_keys = set()
      state = 'header'
      empty = True
      header_lines = []
      stdout_lines = []
      stderr_lines = []
      for line in f.readlines():
        empty = False
        m = re.match(r'\s*#(.*)$', line)
        if m:
          if state == 'stdout':
            raise Error('unexpected directive in STDOUT block: %s' % line)

          directive = m.group(1).strip()
          if directive.lower() == 'stdout:':
            if 'stdout_file' in seen_keys:
              raise Error('can\'t have stdout section and stdout file')
            state = 'stdout'
            continue

          if state != 'header':
            raise Error('unexpected directive: %s' % line)

          key, value = directive.split(':')
          key = key.strip().lower()
          value = value.strip()
          if key in seen_keys:
            raise Error('%s already set' % key)
          seen_keys.add(key)
          if key == 'exe':
            self.exe = value
          elif key == 'flags':
            self.flags = shlex.split(value)
          elif key == 'file':
            self.pexe = value
          elif key == 'error':
            self.expected_error = int(value)
          elif key == 'args':
            self.args = shlex.split(value)
          elif key == 'stdout_file':
            self.stdout_file = value
            with open(self.stdout_file) as s:
              self.expected_stdout = s.read()
          elif key == 'slow':
            self.slow = True
          else:
            raise Error('Unknown directive: %s' % key)
        elif state == 'header':
          state = 'stderr'

        if state == 'header':
          header_lines.append(line)
        elif state == 'stderr':
          stderr_lines.append(line)
        elif state == 'stdout':
          stdout_lines.append(line)
    if empty:
      raise Error('empty test file')

    self.header = ''.join(header_lines)
    if not self.stdout_file:
      self.expected_stdout = ''.join(stdout_lines)
    self.expected_stderr = ''.join(stderr_lines)

  def GetExecutable(self, override_exe):
    if override_exe:
      exe = override_exe
    elif self.exe:
      exe = os.path.join(REPO_ROOT_DIR, self.exe)
    else:
      exe = DEFAULT_PNACL_EXE

    return os.path.relpath(exe, SCRIPT_DIR)

  def GetCommand(self, override_exe=None):
    cmd = [self.GetExecutable(override_exe)]
    cmd += self.flags
    cmd += AsList(self.pexe)
    cmd += ['--'] + AsList(self.args)
    return cmd

  def Run(self, override_exe=None):
    # Pass 'pnacl' as the executable name so the output is consistent
    cmd = ['pnacl'] + self.GetCommand(override_exe)[1:]
    exe = self.GetExecutable(override_exe)
    try:
      start_time = time.time()
      process = subprocess.Popen(cmd, executable=exe, stdout=subprocess.PIPE,
                                                      stderr=subprocess.PIPE)
      stdout, stderr = process.communicate()
      duration = time.time() - start_time
    except OSError as e:
      raise Error(str(e))

    return stdout, stderr, process.returncode, duration

  def Rebase(self, stdout, stderr):
    with open(self.name, 'w') as f:
      f.write(self.header)
      f.write(stderr)
      if self.stdout_file:
        with open(self.stdout_file, 'w') as s:
          s.write(stdout)
      elif stdout:
        f.write('# STDOUT:\n')
        f.write(stdout)

  def Diff(self, stdout, stderr):
    if self.expected_stderr != stderr:
      diff_lines = DiffLines(self.expected_stderr, stderr)
      raise Error('stderr mismatch:\n' + ''.join(diff_lines))

    if self.expected_stdout != stdout:
      if self.stdout_file:
        raise Error('stdout binary mismatch')
      else:
        diff_lines = DiffLines(self.expected_stdout, stdout)
        raise Error('stdout mismatch:\n' + ''.join(diff_lines))


def PrintStatus(passed, failed, start_time, incremental):
  total_duration = time.time() - start_time
  if incremental:
    sys.stderr.write('\r')
  status = '[+%d|-%d] (%.2fs)' % (passed, failed, total_duration)
  PrintStatus.last_status_length = len(status)
  sys.stderr.write(status)
  if not incremental:
    sys.stderr.write('\n')
  sys.stderr.flush()

PrintStatus.last_status_length = 0


def ClearStatus():
  sys.stderr.write('\r%s\r' % (' ' * PrintStatus.last_status_length))


def main(args):
  parser = argparse.ArgumentParser()
  parser.add_argument('-e', '--executable', help='override executable.')
  parser.add_argument('-v', '--verbose', help='print more diagnotic messages. '
                      'Use more than once for more info.', action='count')
  parser.add_argument('-l', '--list', help='list all tests.',
                      action='store_true')
  parser.add_argument('-r', '--rebase',
                      help='rebase a test to its current output.',
                      action='store_true')
  parser.add_argument('-s', '--slow', help='run slow tests.',
                      action='store_true')
  parser.add_argument('patterns', metavar='pattern', nargs='*',
                      help='test patterns.')
  options = parser.parse_args(args)

  if options.patterns:
    pattern_re = '|'.join(fnmatch.translate('*%s*' % p)
                          for p in options.patterns)
  else:
    pattern_re = '.*'

  if options.verbose >= 2:
    level=logging.DEBUG
  elif options.verbose:
    level=logging.INFO
  else:
    level=logging.WARNING
  logging.basicConfig(level=level, format='%(message)s')

  tests = FindTestFiles(SCRIPT_DIR, '.txt', pattern_re)
  if options.list:
    for test in tests:
      print test
    return 0

  if options.executable:
    if not os.path.exists(options.executable):
      parser.error('executable %s does not exist' % options.executable)

  run_cwd = os.getcwd()
  os.chdir(SCRIPT_DIR)

  isatty = os.isatty(1)
  short_display = not logger.isEnabledFor(logging.INFO)

  passed = 0
  failed = 0
  failed_tests = []
  start_time = time.time()
  for test in tests:
    info = TestInfo()
    try:
      info.Parse(test)
      if not options.slow and info.slow:
        logger.info('. %s (skipped)' % info.name)
        continue

      stdout, stderr, returncode, duration = info.Run(options.executable)
      if returncode != info.expected_error:
        # This test has already failed, but diff it anyway.
        msg = 'expected error code %d, got %d.' % (info.expected_error,
                                                   returncode)
        try:
          info.Diff(stdout, stderr)
        except Error as e:
          msg += '\n' + str(e)

        raise Error(msg)

      if options.rebase:
        info.Rebase(stdout, stderr)
      else:
        info.Diff(stdout, stderr)

      passed += 1
      logger.info('+ %s (%.3fs)' % (info.name, duration))
    except Error as e:
      failed_tests.append(info)
      failed += 1
      msg = ''
      cmd = info.GetCommand(options.executable)
      msg += Indent(str(e), 2)
      if short_display:
        ClearStatus()
      logger.error('- %s\n%s' % (info.name, msg))

    if short_display:
      PrintStatus(passed, failed, start_time, True)

  if short_display:
    ClearStatus()

  ret = 0

  if failed:
    logging.error('**** FAILED %s' % ('*' * (80 - 14)))
    for info in failed_tests:
      name = info.name
      cmd = info.GetCommand(options.executable)
      exe = os.path.relpath(info.GetExecutable(options.executable), run_cwd)
      msg = Indent('cmd = (cd %s && %s)\n' % (
          os.path.relpath(SCRIPT_DIR, run_cwd), ' '.join(cmd)), 2)
      msg += Indent('rerun = %s\n' % ' '.join(
          [sys.executable, sys.argv[0], '-e', exe, name]), 2)
      logger.error('- %s\n%s' % (name, msg))
    ret = 1

  PrintStatus(passed, failed, start_time, False)
  return ret


if __name__ == '__main__':
  try:
    sys.exit(main(sys.argv[1:]))
  except Error as e:
    sys.stderr.write(str(e) + '\n')
    sys.exit(1)
