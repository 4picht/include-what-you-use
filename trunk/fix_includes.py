#!/usr/bin/python

##===--- fix_includes.py - rewrite source files based on iwyu output -------===##
#
#                     The LLVM Compiler Infrastructure
#
# This file is distributed under the University of Illinois Open Source
# License. See LICENSE.TXT for details.
#
##===-----------------------------------------------------------------------===##

"""Update files with the 'correct' #include and forward-declare lines.

Given the output of include_what_you_use on stdin -- when run at the
(default) --v=1 verbosity level or higher -- modify the files
mentioned in the output, removing their old #include lines and
replacing them with the lines given by the include_what_you_use
script.

We only edit files that are writeable (presumably open for p4 edit),
unless the user supplies a command to make files writeable via the
--checkout_command flag (eg '--checkout_command="p4 edit"').


This script runs in four stages.  In the first, it groups physical
lines together to form 'move spans'.  A 'move span' is the atomic unit
for moving or deleting code.  A move span is either a) an #include
line, along with any comment lines immediately preceding it; b) a
forward-declare line -- or more if it's a multi-line forward declare
-- along with preceding comments; c) any other single line.  Example:

   // I really am glad I'm forward-declaring this class!
   // If I didn't, I'd have to #include the entire world.
   template<typename A, typename B, typename C, typename D>
   class MyClass;

Then, it groups move spans together into 'reorder spans'.  These are
spans of code that consist entirely of #includes and forward-declares,
maybe separated by blank lines and comments.  We assume that we can
arbitrarily reorder #includes and forward-declares within a reorder
span, without affecting correctness.  Things like #ifdefs, #defines,
namespace declarations, static variable declarations, class
definitions, etc -- just about anything -- break up reorder spans.

In stage 3 it deletes all #include and forward-declare lines that iwyu
says to delete.  iwyu includes line numbers for deletion, making this
part easy.  If this step results in "empty" #ifdefs or namespaces
(#ifdefs or namespaces with no code inside them), we delete those as
well.  We recalculate the reorder spans, which may have gotten bigger
due to the deleted code.

In stage 4 it adds new iwyu-dictated #includes and forward-declares
after the last existing #includes and forward-declares.  Then it
reorders the #includes and forward-declares to match the order
specified by iwyu.  It follows iwyu's instructions as much as
possible, modulo the constraint that an #include or forward-declare
cannot leave its current reorder span.

All this moving messes up the blank lines, which we then need to fix
up.  Then we're done!
"""

__author__ = 'csilvers@google.com (Craig Silverstein)'

import difflib
import optparse
import os
import re
import sys

_USAGE = """\
%prog [options] [filename] ... < <output from include-what-you-use script>
    OR %prog -s [other options] <filename> ...

%prog reads the output from the include-what-you-use
script on stdin -- run with --v=1 (default) verbose or above -- and,
unless --sort_only or --dry_run is specified,
modifies the files mentioned in the output, removing their old
#include lines and replacing them with the lines given by the
include_what_you_use script.  It also sorts the #include and
forward-declare lines.

Only writable files (those opened for p4 edit) are modified (unless
--checkout_command is specified).  All files mentioned in the
include-what-you-use script are modified, unless filenames are
specified on the commandline, in which case only those files are
modified.

The exit code is the number of files that were modified (or that would
be modified if --dry_run was specified) unless that number exceeds 100,
in which case 100 is returned.
"""

_COMMENT_RE = re.compile('\s*//.*')

# For this to be a header guard, the two groups from these re's must match.
_HEADER_GUARD_RE = re.compile(r'^\s*#\s*(?:ifndef|if\s*!\s*defined)\s*\(*\s*'
                              r'([\w_]+)')
_HEADER_GUARD_NEXTLINE_RE = re.compile(r'^\s*#\s*define\s+([\w_]+)')

# These are the types of lines a file can have.  These are matched
# using re.match(), so don't need a leading ^.
_C_COMMENT_START_RE = re.compile(r'\s*/\*')
_C_COMMENT_END_RE = re.compile(r'.*\*/\s*$')
_COMMENT_LINE_RE = re.compile(r'\s*//')
_BLANK_LINE_RE = re.compile(r'\s*$')
_IFDEF_RE = re.compile(r'\s*#\s*if')            # compiles #if/ifdef/ifndef
_ELSE_RE = re.compile(r'\s*#\s*(else|elif)\b')  # compiles #else/elif
_ENDIF_RE = re.compile(r'\s*#\s*endif\b')
# This is used to delete 'empty' namespaces after fwd-decls are removed.
# Some third-party libraries use macros to start/end namespaces.
_NAMESPACE_START_RE = re.compile(r'(\s*namespace\b[^{]*{\s*)+(//.*)?$|'
                                 r'(U_NAMESPACE_BEGIN)')
_NAMESPACE_END_RE = re.compile(r'(})|(U_NAMESPACE_END)')
# The group (in parens) holds the unique 'key' identifying this #include.
_INCLUDE_RE = re.compile(r'\s*#\s*include\s+([<"][^"">]+[>"])')
# We don't need this to actually match forward-declare lines (we get
# that information from the iwyu input), but we do need an RE here to
# serve as an index to _LINE_TYPES.  So we use an RE that never matches.
_FORWARD_DECLARE_RE = re.compile(r'$.')

# We annotate every line in the source file by the re it matches, or None.
_LINE_TYPES = [_COMMENT_LINE_RE, _BLANK_LINE_RE,
               _NAMESPACE_START_RE, _NAMESPACE_END_RE,
               _IFDEF_RE, _ELSE_RE, _ENDIF_RE,
               _INCLUDE_RE, _FORWARD_DECLARE_RE,
              ]

# A regexp matching #include lines that should not be sorted -- that
# is, we should never reorganize the code so an #include that used to
# come before this line now comes after, or vice versa.  This can be
# used for 'fragile' #includes that require other #includes to happen
# before them to function properly.
_NOSORT_INCLUDES = re.compile(r'^\s*#\s*include\s+(<linux/)')


class FixIncludesError(Exception):
  pass


class IWYUOutputRecord(object):
  """Information that the iwyu output file has about one source file."""

  def __init__(self, filename):
    self.filename = filename

    # A set of integers.
    self.lines_to_delete = set()

    # A set of integer line-numbers, for each #include iwyu saw that
    # is marked with a line number.  This is usually not an exhaustive
    # list of include-lines, but that's ok because we only use this
    # data structure for sanity checking: we double-check with our own
    # analysis that these lines are all # #include lines.  If not, we
    # know the iwyu data is likely out of date, and we complain.  So
    # more data here is always welcome, but not essential.
    self.some_include_lines = set()

    # A set of integer line-number spans [start_line, end_line), for
    # each forward-declare iwyu saw.  iwyu reports line numbers for
    # every forward-declare it sees in the source code.  (It won't
    # report, though, forward-declares inside '#if 0' or similar.)
    self.seen_forward_declare_lines = set()

    # A set of each line in the iwyu 'add' section.
    self.includes_and_forward_declares_to_add = set()

    # A map from the include filename (including ""s or <>s) to the
    # full line as given by iwyu, which includes comments that iwyu
    # has put next to the #include.  This holds both 'to-add' and
    # 'to-keep' #includes.  If flags.comments is False, the comments
    # are removed before adding to this list.
    self.full_include_lines = {}

  def Merge(self, other):
    """Merges other with this one.  They must share a filename.

    This function is intended to be used when we see two iwyu records
    in the input, both for the same file.  We can merge the two together.
    We are conservative: we union the lines to add, and intersect the
    lines to delete.

    Arguments:
      other: an IWYUOutputRecord to merge into this one.
        It must have the same value for filename that self does.
    """
    assert self.filename == other.filename, "Can't merge distinct files"
    self.lines_to_delete.intersection_update(other.lines_to_delete)
    self.some_include_lines.update(other.some_include_lines)
    self.seen_forward_declare_lines.update(other.seen_forward_declare_lines)
    self.includes_and_forward_declares_to_add.update(
        other.includes_and_forward_declares_to_add)
    self.full_include_lines.update(other.full_include_lines)

  def __str__(self):
    return ('--- iwyu record ---\n  FILENAME: %s\n  LINES TO DELETE: %s\n'
            '  (SOME) INCLUDE LINES: %s\n  (SOME) FWD-DECL LINES: %s\n'
            '  TO ADD: %s\n  ALL INCLUDES: %s\n---\n'
            % (self.filename, self.lines_to_delete,
               self.some_include_lines, self.seen_forward_declare_lines,
               self.includes_and_forward_declares_to_add,
               self.full_include_lines))


class IWYUOutputParser(object):
  """Parses the lines in iwyu output corresponding to one source file."""

  # iwyu adds this comment to some lines to map them to the source file.
  _LINE_NUMBERS_COMMENT_RE = re.compile(r'\s*// lines ([0-9]+)-([0-9]+)')

  # The output of include-what-you-use has sections that indicate what
  # #includes and forward-declares should be added to the output file,
  # what should be removed, and what the end result is.  The first line
  # of each section also has the filename.
  _ADD_SECTION_RE = re.compile(r'^(.*) should add these lines:$')
  _REMOVE_SECTION_RE = re.compile(r'^(.*) should remove these lines:$')
  _TOTAL_SECTION_RE = re.compile(r'^The full include-list for ([^:]*):$')
  _SECTION_END_RE = re.compile(r'^---$')

  # Alternately, if a file does not need any iwyu modifications (though
  # it still may need its #includes sorted), iwyu will emit this:
  _NO_EDITS_RE = re.compile(r'^\((.*) has correct #includes/fwd-decls\)$')

  _RE_TO_NAME = {_ADD_SECTION_RE: 'add',
                 _REMOVE_SECTION_RE: 'remove',
                 _TOTAL_SECTION_RE: 'total',
                 _SECTION_END_RE: 'end',
                 _NO_EDITS_RE: 'no_edits',
                }
  # A small state-transition machine.  key==None indicates the start
  # state.  value==None means that the key is an end state (that is,
  # its presence indicates the record is finished).
  _EXPECTED_NEXT_RE = {
      None:               frozenset([_ADD_SECTION_RE, _NO_EDITS_RE]),
      _ADD_SECTION_RE:    frozenset([_REMOVE_SECTION_RE]),
      _REMOVE_SECTION_RE: frozenset([_TOTAL_SECTION_RE]),
      _TOTAL_SECTION_RE:  frozenset([_SECTION_END_RE]),
      _SECTION_END_RE:    None,
      _NO_EDITS_RE:       None,
  }

  def __init__(self):
    # This is set to one of the 'section' REs above.  None is the start-state.
    self.current_section = None
    self.filename = '<unknown file>'
    self.lines_by_section = {}     # key is an RE, value is a list of lines

  def _ProcessOneLine(self, line):
    """Reads one line of input, updates self, and returns False at EORecord.

    If the line matches one of the hard-coded section names, updates
    self.filename and self.current_section.  Otherwise, the line is
    taken to be a member of the currently active section, and is added
    to self.lines_by_section.

    Arguments:
      line: one line from the iwyu input file.

    Returns:
      False if the line is the end-of-section marker, True otherwise.

    Raises:
      FixIncludesError: if there is an out-of-order section or
      mismatched filename.
    """
    line = line.rstrip()     # don't worry about line endings
    if not line:             # just ignore blank lines
      return True

    for (section_re, section_name) in self._RE_TO_NAME.iteritems():
      m = section_re.search(line)
      if m:
        # Check or set the filename (if the re has a group, it's for filename).
        if section_re.groups >= 1:
          this_filename = m.group(1)
          if (self.current_section is not None and
              this_filename != self.filename):
            raise FixIncludesError('"%s" section for %s comes after "%s" for %s'
                                   % (section_name, this_filename,
                                      self._RE_TO_NAME[self.current_section],
                                      self.filename))
          self.filename = this_filename

        # Check and set the new section we're entering.
        if section_re not in self._EXPECTED_NEXT_RE[self.current_section]:
          if self.current_section is None:
            raise FixIncludesError('%s: "%s" section unexpectedly comes first'
                                   % (self.filename, section_name))
          else:
            raise FixIncludesError('%s: "%s" section unexpectedly follows "%s"'
                                   % (self.filename, section_name,
                                      self._RE_TO_NAME[self.current_section]))
        self.current_section = section_re
        # We're done parsing this record if this section has nothing after it.
        return self._EXPECTED_NEXT_RE[self.current_section] is not None

    # We're not starting a new section, so just add to the current section.
    # We ignore lines before section-start, they're probably things like
    # compiler messages ("Compiling file foo").
    if self.current_section is not None:
      self.lines_by_section.setdefault(self.current_section, []).append(line)
    return True

  def ParseOneRecord(self, iwyu_output, flags):
    """Given a file object with output from an iwyu run, return per file info.

    For each source file that iwyu_output mentions (because iwyu was run on
    it), we return a structure holding the information in IWYUOutputRecord:
    1) What file these changes apply to
    2) What line numbers hold includes/fwd-declares to remove
    3) What includes/fwd-declares to add
    4) Ordering information for includes and fwd-declares

    Arguments:
      iwyu_output: a File object returning lines from an iwyu run
      flags: commandline flags, as parsed by optparse.  We use
         flags.comments, which controls whether we output comments
         generated by iwyu.
    Returns:
       An IWYUOutputRecord object, or None at EOF.

    Raises:
       FixIncludesError: for malformed-looking lines in the iwyu output.
    """
    for line in iwyu_output:
      if not self._ProcessOneLine(line):   # returns False at end-of-record
        break
    else:                                  # for/else
      return None                          # at EOF

    # Now set up all the fields in an IWYUOutputRecord.
    # IWYUOutputRecord.filename
    retval = IWYUOutputRecord(self.filename)

    # IWYUOutputRecord.lines_to_delete
    for line in self.lines_by_section.get(self._REMOVE_SECTION_RE, []):
      m = self._LINE_NUMBERS_COMMENT_RE.search(line)
      if not m:
        raise FixIncludesError('line "%s" (for %s) has no line number'
                               % (line, self.filename))
      # The RE is of the form [start_line, end_line], inclusive.
      for line_number in xrange(int(m.group(1)), int(m.group(2)) + 1):
        retval.lines_to_delete.add(line_number)

    # IWYUOutputRecord.some_include_lines
    for line in (self.lines_by_section.get(self._REMOVE_SECTION_RE, []) +
                 self.lines_by_section.get(self._TOTAL_SECTION_RE, [])):
      if not _INCLUDE_RE.match(line):
        continue
      m = self._LINE_NUMBERS_COMMENT_RE.search(line)
      if not m:
        continue   # not all #include lines have line numbers, but some do
      for line_number in xrange(int(m.group(1)), int(m.group(2)) + 1):
        retval.some_include_lines.add(line_number)

    # IWYUOutputRecord.seen_forward_declare_lines
    for line in (self.lines_by_section.get(self._REMOVE_SECTION_RE, []) +
                 self.lines_by_section.get(self._TOTAL_SECTION_RE, [])):
      # Everything that's not an #include is a forward-declare.
      if line.startswith('- '):    # the 'remove' lines all start with '- '.
        line = line[len('- '):]
      if _INCLUDE_RE.match(line):
        continue
      m = self._LINE_NUMBERS_COMMENT_RE.search(line)
      if m:
        retval.seen_forward_declare_lines.add((int(m.group(1)),
                                               int(m.group(2))+1))

    # IWYUOutputRecord.includes_and_forward_declares_to_add
    for line in self.lines_by_section.get(self._ADD_SECTION_RE, []):
      if not flags.comments:
        line = _COMMENT_RE.sub('', line)
      retval.includes_and_forward_declares_to_add.add(line)

    # IWYUOutputRecord.full_include_lines
    for line in self.lines_by_section.get(self._TOTAL_SECTION_RE, []):
      m = _INCLUDE_RE.match(line)
      if m:
        if not flags.comments:
          line = _COMMENT_RE.sub('', line)  # pretend there were no comments
        else:
          # Just remove '// line XX': that's iwyu metadata, not a real comment
          line = self._LINE_NUMBERS_COMMENT_RE.sub('', line)
        retval.full_include_lines[m.group(1)] = line

    return retval


class LineInfo(object):
  """Information about a single line of a source file."""

  def __init__(self, line):
    """Initializes the content of the line, but no ancillary fields."""
    # The content of the line in the input file
    self.line = line

    # The 'type' of the line.  The 'type' is one of the regular
    # expression objects in _LINE_TYPES, or None for any line that
    # does not match any regular expression in _LINE_TYPES.
    self.type = None

    # Set to true if we want to delete/ignore this line in the output
    # (for instance, because iwyu says to delete this line).  At the
    # start, the only line to delete is the 'dummy' line 0.
    self.deleted = self.line is None

    # If this line is an #include or a forward-declare, gives a
    # [begin,end) pair saying the 'span' this line is part of.  We do
    # this for two types of span: the move span (an #include or
    # forward declare, along with any preceding comments) and the
    # reorder span (a continguous block of move-spans, connected only
    # by blank lines and comments).  For lines that are not an
    # #include or forward-declare, these may have an arbitrary value.
    self.move_span = None
    self.reorder_span = None

    # If this line is an #include or a forward-declare, gives the
    # 'key' of the line.  For #includes it is the filename included,
    # including the ""s or <>s.  For a forward-declare it's the name
    # of the class/struct.  For other types of lines, this is None.
    self.key = None

  def __str__(self):
    if self.deleted:
      line = 'XX-%s-XX' % self.line
    else:
      line = '>>>%s<<<' % self.line
    if self.type is None:
      type_id = None
    else:
      type_id = _LINE_TYPES.index(self.type)
    return ('%s\n  -- type: %s (key: %s).  move_span: %s.  reorder_span: %s'
            % (line, type_id, self.key, self.move_span, self.reorder_span))


def _ReadFile(filename):
  """Read from filename and return a list of file lines."""
  try:
    return open(filename).read().splitlines()
  except (IOError, OSError), why:
    print "Skipping '%s': %s" % (filename, why)
  return None


def _ReadWriteableFile(filename, ignore_writeable):
  """Read from filename and return a list of file lines.

  Given a filename, if the file is found and is writable, read
  the file contents and return it as a list of lines (newlines
  removed).  If the file is not found or is not writable, or if
  there is another IO error, return None.

  Arguments:
    filename: the name of the file to read.
    ignore_writeable: if True, don't check whether the file is writeable;
       return the contents anyway.

  Returns:
    A list of lines (without trailing newline) from filename, or None
    if the file is not writable, or cannot be read.
  """
  if os.access(filename, os.W_OK) or ignore_writeable:
    return _ReadFile(filename)
  return None


def _WriteFileContentsToFileObject(f, file_lines):
  """Write the given file-lines to the file."""
  f.write('\n'.join(file_lines))
  f.write('\n')


def _WriteFileContents(filename, file_lines):
  """Write the given file-lines to the file."""
  try:
    f = open(filename, 'w')
    try:
      _WriteFileContentsToFileObject(f, file_lines)
    finally:
      f.close()
  except (IOError, OSError), why:
    print "Error writing '%s': %s" % (filename, why)


def _RunCommandOnFile(command, filename):
  """Run the given shell command on the given filename (appended to end)."""
  # Replace ' with '"'"' in the filename, since it's inside single quotes.
  os.system("%s '%s'" % (command, filename.replace("'", "'\"'\"'")))


def _WriteFileContentsIfPossible(filename, file_lines, checkout_command):
  """Write the given file-lines to the file, if it can be written.

  Arguments:
    filename: the name of the file to write.
    file_lines: iterable collection of lines.
    checkout_command: a shell command to run on the file if it is not
       writeable.  The command should be a string, to which we append
       the filename to edit; for example, "p4 edit".  If empty or None,
       no checkout command is run.
  """
  if checkout_command and not os.access(filename, os.W_OK):
    # TODO(user): Per wan@google.com, it would be MUCH faster to run
    # a single checkout command for all files that need to be checked
    # out, rather than one command for each file.
    _RunCommandOnFile(checkout_command, filename)
  _WriteFileContents(filename, file_lines)


def PrintFileDiff(old_file_contents, new_file_contents):
  """Print a unified diff between files, specified as lists of lines."""
  diff = difflib.unified_diff(old_file_contents, new_file_contents)
  # skip the '--- <filename>/+++ <filename>' lines at the start
  try:
    diff.next()
    diff.next()
    print '\n'.join(diff)
  except StopIteration:
    pass


def _CalculateLineTypesAndKeys(file_lines, iwyu_record):
  """Fills file_line's type and key fields, where the 'type' is a regexp object.

  We match each line (line_info.line) against every regexp in
  _LINE_TYPES, and assign the first that matches, or None if none
  does.  We also use iwyu_record's some_include_lines and
  seen_forward_declare_lines to identify those lines.  In fact,
  that's the only data source we use for forward-declare lines.

  Arguments:
    file_lines: an array of LineInfo objects with .line fields filled in.
    iwyu_record: the IWYUOutputRecord struct for this source file.

  Raises:
    FixIncludesError: if iwyu_record's line-number information is
      is inconsistent with what we see in the file.  (For instance,
      it says line 12 is an #include, but we say it's a blank line,
      or the file only has 11 lines.)
  """
  in_c_style_comment = False
  for line_info in file_lines:
    if line_info.line is None:
      line_info.type = None
    elif _C_COMMENT_START_RE.match(line_info.line):
      line_info.type = _COMMENT_LINE_RE
      if not _C_COMMENT_END_RE.match(line_info.line):   # not a 1-line comment
        in_c_style_comment = True
    elif in_c_style_comment and _C_COMMENT_END_RE.match(line_info.line):
      line_info.type = _COMMENT_LINE_RE
      in_c_style_comment = False
    elif in_c_style_comment:
      line_info.type = _COMMENT_LINE_RE
    else:
      for type_re in _LINE_TYPES:
        m = type_re.match(line_info.line)
        if m:
          line_info.type = type_re
          if type_re == _INCLUDE_RE:
            line_info.key = m.group(1)   # get the 'key' for the #include.
          break
      else:    # for/else
        line_info.type = None   # means we didn't match any re

  # Now double-check against iwyu that we got all the #include lines right.
  for line_number in iwyu_record.some_include_lines:
    if file_lines[line_number].type != _INCLUDE_RE:
      raise FixIncludesError('iwyu line number %s:%d (%s) is not an #include'
                             % (iwyu_record.filename, line_number,
                                file_lines[line_number].line))

  # We depend entirely on the iwyu_record for the forward-declare lines.
  for (start_line, end_line) in iwyu_record.seen_forward_declare_lines:
    for line_number in xrange(start_line, end_line):
      if line_number >= len(file_lines):
        raise FixIncludesError('iwyu line number %s:%d is past file-end'
                               % (iwyu_record.filename, line_number))
      file_lines[line_number].type = _FORWARD_DECLARE_RE

  # While we're at it, let's do a bit more sanity checking on iwyu_record.
  for line_number in iwyu_record.lines_to_delete:
    if line_number >= len(file_lines):
      raise FixIncludesError('iwyu line number %s:%d is past file-end'
                             % (iwyu_record.filename, line_number))
    elif file_lines[line_number].type not in (_INCLUDE_RE,
                                              _FORWARD_DECLARE_RE):
      raise FixIncludesError('iwyu line number %s:%d (%s) is not'
                             ' an #include or forward declare'
                             % (iwyu_record.filename, line_number,
                                file_lines[line_number].line))


def _PreviousNondeletedLine(file_lines, line_number):
  """Returns the line number of the previous not-deleted line, or None."""
  for line_number in xrange(line_number - 1, -1, -1):
    if not file_lines[line_number].deleted:
      return line_number
  return None


def _NextNondeletedLine(file_lines, line_number):
  """Returns the line number of the next not-deleted line, or None."""
  for line_number in xrange(line_number + 1, len(file_lines)):
    if not file_lines[line_number].deleted:
      return line_number
  return None


def _LineNumberStartingPrecedingComments(file_lines, line_number):
  """Returns the line-number for the comment-lines preceding the given linenum.

  Looking at file_lines, look at the lines immediately preceding the
  given line-number.  If they're comment lines, return the first line
  of the comment lines preceding the given line.  Otherwise, return
  the given line number.

  As a special case, if the comments go all the way up to the first
  line of the file (line 1), we assume they're comment lines, which
  are special -- they're not associated with any source code line --
  and we return line_number in that case.

  Arguments:
    file_lines: an array of LineInfo objects, with .type fields filled in.
    line_number: an index into file_lines.

  Returns:
    The first line number of the preceding comments, or line_number
      if there are no preceding comments or they appear to be a
      top-of-file copyright notice.
  """
  retval = line_number
  while retval > 0 and file_lines[retval - 1].type == _COMMENT_LINE_RE:
    retval -= 1
  if retval <= 1:          # top-of-line comments
    retval = line_number   # so ignore all the comment lines
  return retval


def _CalculateMoveSpans(file_lines, forward_declare_spans):
  """Fills each input_line's move_span field.

  A 'move span' is a range of lines (from file_lines) that includes
  an #include or forward-declare, and all the comments preceding it.
  It is the unit we would move if we decided to move (or delete) this
  #include or forward-declare.

  For lines of type _INCLUDE_RE or _FORWARD_DECLARE_RE, the move span
  is set to the tuple [start_of_span, end_of_span).  All other lines
  have the move span kept at None.

  Arguments:
    file_lines: an array of LineInfo objects, with .type fields filled in.
    forward_declare_spans: a set of line-number pairs
       [start_line, end_line), each representing a single namespace.
       In practice this comes from iwyu_record.seen_forward_declare_lines.
  """
  # First let's do #includes.
  for line_number in xrange(len(file_lines)):
    if file_lines[line_number].type == _INCLUDE_RE:
      span_begin = _LineNumberStartingPrecedingComments(file_lines, line_number)
      for i in xrange(span_begin, line_number + 1):
        file_lines[i].move_span = (span_begin, line_number + 1)

  # Now forward-declares.  These spans come as input to this function.
  for (span_begin, span_end) in forward_declare_spans:
    span_begin = _LineNumberStartingPrecedingComments(file_lines, span_begin)
    for i in xrange(span_begin, span_end):
      file_lines[i].move_span = (span_begin, span_end)


def _IsNosortInclude(file_lines, line_range):
  """Returns true iff some line in [line_range[0], line_range[1]) is _NOSORT."""
  for line_number in apply(xrange, line_range):
    if (not file_lines[line_number].deleted and
        _NOSORT_INCLUDES.search(file_lines[line_number].line)):
      return True
  return False


def _LinesAreAllBlank(file_lines, start_line, end_line):
  """Returns true iff all lines in [start_line, end_line) are blank/deleted."""
  for line_number in xrange(start_line, end_line):
    if (not file_lines[line_number].deleted and
        file_lines[line_number].type != _BLANK_LINE_RE):
      return False
  return True


def _CalculateReorderSpans(file_lines):
  """Fills each input_line's reorder_span field.

  A 'reorder span' is a range of lines (from file_lines) that only has
  #includes and forward-declares in it (and maybe blank lines, and
  comments associated with #includes or forward-declares).  In
  particular, it does not include any "real code" besides #includes
  and forward-declares: no functions, no static variable assignment,
  no macro #defines, no nothing.  We are willing to reorder #includes
  and namespaces freely inside a reorder span.

  Calculating reorder_span is easy: they're just the union of
  contiguous move-spans (with perhaps blank lines and comments
  thrown in), because move-spans share the 'no actual code'
  requirement.

  There's one exception: if any move-span matches the _NOSORT_INCLUDES
  regexp, it means that we should consider that move-span to be a
  'barrier': nothing should get reordered from one side of that
  move-span to the other.  (This is used for #includes that depend on
  other #includes being before them to function properly.)  We do that
  by putting them into their own reorder span.

  For lines of type _INCLUDE_RE or _FORWARD_DECLARE_RE, the reorder
  span is set to the tuple [start_of_span, end_of_span).  All other
  lines have an arbitrary value for the reorder span.

  Arguments:
    file_lines: an array of LineInfo objects with .type and .move_span
       fields filled in.
  """
  # Happily, move_spans are disjoint. Just make sure they're sorted and unique.
  move_spans = [s.move_span for s in file_lines if s.move_span is not None]
  sorted_move_spans = sorted(set(move_spans))

  i = 0
  while i < len(sorted_move_spans):
    reorder_span_start = sorted_move_spans[i][0]

    # If we're a 'nosort' include, we're always in a reorder span of
    # our own.  Otherwise, add in the next move span if we're
    # connected to it only by blank lines.
    if not _IsNosortInclude(file_lines, sorted_move_spans[i]):
      while i < len(sorted_move_spans) - 1:
        move_span_end = sorted_move_spans[i][1]
        next_move_span_start = sorted_move_spans[i+1][0]
        if (_LinesAreAllBlank(file_lines, move_span_end, next_move_span_start)
            and not _IsNosortInclude(file_lines, sorted_move_spans[i+1])):
          i += 1
        else:
          break
    reorder_span_end = sorted_move_spans[i][1]
    # We'll map every line in the span to the span-extent.
    for line_number in xrange(reorder_span_start, reorder_span_end):
      file_lines[line_number].reorder_span = (reorder_span_start,
                                              reorder_span_end)
    i += 1


def ParseOneFile(f, iwyu_record):
  """Given a file object, read and classify the lines of the file.

  For each file that iwyu_output mentions, we return a list of LineInfo
  objects, which is a parsed version of each line, including not only
  its content but its 'type', its 'key', etc.

  Arguments:
    f: an iterable object returning lines from a file.
    iwyu_record: the IWYUOutputRecord struct for this source file.

  Returns:
     An array of LineInfo objects.  The first element is always a dummy
     element, so the first line of the file is at retval[1], matching
     the way iwyu counts line numbers.
  """
  file_lines = [LineInfo(None)]
  for line in f:
    file_lines.append(LineInfo(line))
  _CalculateLineTypesAndKeys(file_lines, iwyu_record)
  _CalculateMoveSpans(file_lines, iwyu_record.seen_forward_declare_lines)
  _CalculateReorderSpans(file_lines)
  return file_lines


def _DeleteEmptyNamespaces(file_lines):
  """Delete namespaces with nothing in them.

  Empty namespaces could be caused by transformations that removed
  forward-declarations:
        namespace foo {
        class Myclass;
        }
     ->
        namespace foo {
        }
  We want to get rid of the 'empty' namespace in this case.

  This routine 'deletes' lines by setting their 'deleted' field to True.

  Arguments:
    file_lines: an array of LineInfo objects with .type fields filled in.

  Returns:
    The number of namespaces deleted.
  """
  num_namespaces_deleted = 0
  start_line = 0
  while start_line < len(file_lines):
    line_info = file_lines[start_line]
    if line_info.deleted or line_info.type != _NAMESPACE_START_RE:
      start_line += 1
      continue
    # Because multiple namespaces can be on one line
    # ("namespace foo { namespace bar { ..."), we need to count.
    # We use the max because line may have 0 '{'s if it's a macro.
    # TODO(csilvers): ignore { in comments.
    namespace_depth = max(line_info.line.count('{'), 1)
    end_line = start_line + 1
    while end_line < len(file_lines):
      line_info = file_lines[end_line]
      if line_info.deleted:
        end_line += 1
      elif line_info.type in (_COMMENT_LINE_RE, _BLANK_LINE_RE):
        end_line += 1                # ignore blank lines
      elif line_info.type == _NAMESPACE_START_RE:     # nested namespace
        namespace_depth += max(line_info.line.count('{'), 1)
        end_line += 1
      elif line_info.type == _NAMESPACE_END_RE:
        namespace_depth -= max(line_info.line.count('}'), 1)
        end_line += 1
        if namespace_depth <= 0:
          # Delete any comments preceding this namespace as well.
          start_line = _LineNumberStartingPrecedingComments(file_lines,
                                                            start_line)
          # And also blank lines.
          while (start_line > 0 and
                 file_lines[start_line-1].type == _BLANK_LINE_RE):
            start_line -= 1
          for line_number in xrange(start_line, end_line):
            file_lines[line_number].deleted = True
          num_namespaces_deleted += 1
          break
      else:   # bail: we're at a line indicating this isn't an empty namespace
        end_line = start_line + 1  # rewind to try again with nested namespaces
        break
    start_line = end_line

  return num_namespaces_deleted


def _DeleteEmptyIfdefs(file_lines):
  """Deletes ifdefs with nothing in them.

  This could be caused by transformations that removed #includes:
        #ifdef OS_WINDOWS
        # include <windows.h>
        #endif
     ->
        #ifdef OS_WINDOWS
        #endif
  We want to get rid of the 'empty' #ifdef in this case.
  We also handle 'empty' #ifdefs with #else, if both sides of
  the #else are empty.  We also handle #ifndef and #if.

  This routine 'deletes' lines by replacing their content with None.

  Arguments:
    file_lines: an array of LineInfo objects with .type fields filled in.

  Returns:
    The number of ifdefs deleted.
  """
  num_ifdefs_deleted = 0
  start_line = 0
  while start_line < len(file_lines):
    if file_lines[start_line].type != _IFDEF_RE:
      start_line += 1
      continue
    end_line = start_line + 1
    while end_line < len(file_lines):
      line_info = file_lines[end_line]
      if line_info.deleted:
        end_line += 1
      elif line_info.type in (_ELSE_RE, _COMMENT_LINE_RE, _BLANK_LINE_RE):
        end_line += 1                # ignore blank lines
      elif line_info.type == _ENDIF_RE:
        end_line += 1
        # Delete any comments preceding this #ifdef as well.
        start_line = _LineNumberStartingPrecedingComments(file_lines,
                                                          start_line)
        # And also blank lines.
        while (start_line > 0 and
               file_lines[start_line-1].type == _BLANK_LINE_RE):
          start_line -= 1
        for line_number in xrange(start_line, end_line):
          file_lines[line_number].deleted = True
        num_ifdefs_deleted += 1
        break
      else:   # bail: we're at a line indicating this isn't an empty ifdef
        end_line = start_line + 1  # rewind to try again with nested #ifdefs
        break
    start_line = end_line

  return num_ifdefs_deleted


def _DeleteDuplicateLines(file_lines, line_ranges):
  """Goes through all lines in line_ranges, and if any are dups, deletes them.

  For all lines in line_ranges, if any is the same as a previously
  seen line, set its deleted bit to True.  The purpose of line_ranges
  is to avoid lines in #ifdefs and namespaces, that may be identical
  syntactically but have different semantics.  Ideally, line_ranges
  should include only 'top-level' lines.

  We ignore lines that consist only of comments (or are blank).  We
  ignore end-of-line comments when comparing lines for equality.
  NOTE: Because our comment-finding RE is primitive, it's best if
  line_ranges covers only #include and forward-declare lines.  In
  particular, it should not cover lines that may have C literal
  strings in them.

  Arguments:
    file_lines: an array of LineInfo objects.
    line_ranges: a list of [start_line, end_line) pairs.
  """
  seen_lines = set()
  for line_range in line_ranges:
    for line_number in apply(xrange, line_range):
      if file_lines[line_number].type in (_BLANK_LINE_RE, _COMMENT_LINE_RE):
        continue
      uncommented_line = _COMMENT_RE.sub('', file_lines[line_number].line)
      if uncommented_line in seen_lines:
        file_lines[line_number].deleted = True
      elif not file_lines[line_number].deleted:
        seen_lines.add(uncommented_line)


def _DeleteExtraneousBlankLines(file_lines, line_range):
  """Deletes extraneous blank lines caused by line deletion.

  Here's a example file:
     class Foo { ... };

     class Bar;

     class Baz { ... }

  If we delete the "class Bar;" line, we also want to delete one of
  the blank lines around it, otherwise we leave two blank lines
  between Foo and Baz which looks bad.  The idea is that if we have
  whitespace on both sides of a deleted span of code, the whitespace
  on one of the sides is 'extraneous'.  In this case, we should delete
  not only 'class Bar;' but also the whitespace line below it.  That
  leaves one blank line between Foo and Bar, like people would expect.

  We're careful to only delete the minimum of the number of blank
  lines that show up on either side.  If 'class Bar' had one blank
  line before it, and one hundred after it, we'd only delete one blank
  line when we delete 'class Bar'.  This matches user's expecatations.

  The situation can get tricky when two deleted spans touch (we might
  think it's safe to delete the whitespace between them when it's
  not).  To be safe, we only do this check when an entire reorder-span
  has been deleted.  So we check the given line_range, and only do
  blank-line deletion if every line in the range is deleted.

  Arguments:
    file_lines: an array of LineInfo objects, with .type filled in.
    line_range: a range [start_line, end_line).  It should correspond
       to a reorder-span.
  """
  # First make sure the entire span is deleted.
  for line_number in apply(xrange, line_range):
    if not file_lines[line_number].deleted:
      return

  before_line = _PreviousNondeletedLine(file_lines, line_range[0])
  after_line = _NextNondeletedLine(file_lines, line_range[1] - 1)
  while (before_line and file_lines[before_line].type == _BLANK_LINE_RE and
         after_line and file_lines[after_line].type == _BLANK_LINE_RE):
    # OK, we've got whitespace on both sides of a deleted span.  We
    # only want to keep whitespace on one side, so delete on the other.
    file_lines[after_line].deleted = True
    before_line = _PreviousNondeletedLine(file_lines, before_line)
    after_line = _NextNondeletedLine(file_lines, after_line)


def _ShouldInsertBlankLine(decorated_move_span, next_decorated_move_span,
                           file_lines, flags):
  """Returns true iff we should insert a blank line between the two spans.

  Given two decorated move-spans, of the form
     (reorder_range, kind, noncomment_lines, all_lines)
  returns true if we should insert a blank line between them.  We
  always put a blank line when transitioning from an #include to a
  forward-declare and back.  When the appropriate commandline flag is
  set, we also put a blank line between the 'main' includes (foo.h)
  and the C/C++ system includes, and another between the system
  includes and the rest of the Google includes.

  If the two move spans are in different reorder_ranges, that means
  the first move_span is at the end of a reorder range.  In that case,
  a different rule for blank lines applies: if the next line is
  contentful (eg 'static int x = 5;'), or a namespace start, we want
  to insert a blank line to separate the move-span from the next
  block.

  Arguments:
    decorated_move_span: a decorated_move_span we may want to put a blank
       line after.
    next_decorated_move_span: the next decorated_move_span, which may
       be a sentinel decorated_move_span at end-of-file.
    file_lines: an array of LineInfo objects with .deleted filled in.
    flags: commandline flags, as parsed by optparse.  We use
       flags.blank_lines, which controls whether we put blank
       lines between different 'kinds' of #includes.

  Returns:
     true if we should insert a blank line after decorated_move_span.
  """
  # First handle the 'at the end of a reorder range' case.
  if decorated_move_span[0] != next_decorated_move_span[0]:
    next_line = _NextNondeletedLine(file_lines, decorated_move_span[0][1] - 1)
    return (next_line and
            file_lines[next_line].type in (_NAMESPACE_START_RE, None))

  # We never insert a blank line between two spans of the same kind.
  # Nor do we ever insert a blank line at EOF.
  (this_kind, next_kind) = (decorated_move_span[1], next_decorated_move_span[1])
  if this_kind == next_kind or next_kind == _EOF_KIND:
    return False

  # We also never insert a blank line between C and C++-style #includes,
  # no matter what the flag value.
  if (this_kind in [_C_SYSTEM_INCLUDE_KIND, _CXX_SYSTEM_INCLUDE_KIND] and
      next_kind in [_C_SYSTEM_INCLUDE_KIND, _CXX_SYSTEM_INCLUDE_KIND]):
    return False

  # Handle the case we're going from an include to fwd declare or
  # back.  If we get here, we can't both be fwd-declares, so it
  # suffices to check if either of us is.
  if this_kind == _FORWARD_DECLARE_KIND or next_kind == _FORWARD_DECLARE_KIND:
    return True

  # Now, depending on the flag, we insert a blank line whenever the
  # kind changes (we handled the one case where a changing kind
  # doesn't introduce a blank line, above).
  if flags.blank_lines:
    return this_kind != next_kind

  return False


def _IsHeaderGuard(line_info, next_line_info):
  """Given two adjacent lines of a file, say whether line 1 is header guard."""
  m1 = _HEADER_GUARD_RE.search(line_info.line or '')
  m2 = _HEADER_GUARD_NEXTLINE_RE.search(next_line_info.line or '')
  return m1 and m2 and m1.group(1) == m2.group(1)


def _GetToplevelReorderSpans(file_lines):
  """Returns a sorted list of all reorder_spans not inside an #ifdef/namespace.

  This routine looks at all the reorder_spans in file_lines, ignores
  reorder spans inside #ifdefs and namespaces -- except for the 'header
  guard' ifdef that encapsulates an entire .h file -- and returns the
  rest in sorted order.

  Arguments:
    file_lines: an array of LineInfo objects with .type and
       .reorder_span filled in.

  Returns:
    A list of [start_line, end_line) reorder_spans.
  """
  in_ifdef = [False] * len(file_lines)   # lines inside an #if
  ifdef_depth = 0
  for line_number in xrange(len(file_lines)):
    line_info = file_lines[line_number]
    if line_info.deleted:
      continue
    if line_info.type == _IFDEF_RE:
      # Ignore header-guard #ifdef.
      if (ifdef_depth == 0 and line_number < len(file_lines) - 1 and
          _IsHeaderGuard(file_lines[line_number], file_lines[line_number+1])):
        pass
      else:
        ifdef_depth += 1
    elif line_info.type == _ENDIF_RE:
      ifdef_depth -= 1
    if ifdef_depth > 0:
      in_ifdef[line_number] = True

  # Figuring out whether a } ends a namespace or some other languague
  # construct is hard, so as soon as we see any 'contentful' line
  # inside a namespace, we assume the entire rest of the file is in
  # the namespace.
  in_namespace = [False] * len(file_lines)
  namespace_depth = 0
  for line_number in xrange(len(file_lines)):
    line_info = file_lines[line_number]
    if line_info.deleted:
      continue
    if line_info.type == _NAMESPACE_START_RE:
      # The 'max' is because the namespace-re may be a macro.
      namespace_depth += max(line_info.line.count('{'), 1)
    elif line_info.type == _NAMESPACE_END_RE:
      namespace_depth -= max(line_info.line.count('}'), 1)
    if namespace_depth > 0:
      in_namespace[line_number] = True
      if line_info.type is None:
        for i in xrange(line_number, len(file_lines)):  # rest of file
          in_namespace[i] = True
        break

  reorder_spans = sorted(set([fl.reorder_span for fl in file_lines]))
  good_reorder_spans = []
  for reorder_span in reorder_spans:
    if reorder_span is None:
      continue
    for line_number in apply(xrange, reorder_span):
      if in_ifdef[line_number] or in_namespace[line_number]:
        break
    else:   # for/else
      good_reorder_spans.append(reorder_span)    # never in ifdef or namespace

  return good_reorder_spans


def _GetFirstNamespaceLevelReorderSpan(file_lines):
  """Returns the first reorder-span inside a namespace, if it's easy to do.

  This routine is meant to handle the simple case where code consists
  of includes and forward-declares, and then a 'namespace my_namespace'
  followed by more forward-declares.  We return the reorder span of
  the inside-namespace forward-declares, which is a good place to
  insert new inside-namespace forward-declares (rather than putting
  these new forward-declares at the top level).

  So it goes through the top of the file, stopping at the first
  'contentful' line.  If that line has the form 'namespace <foo> {',
  it then continues until it finds a forward-declare line, or a
  non-namespace contentful line.  In the latter case it returns None.
  In the former case, it figures out the reorder-span this
  forward-declare line is part of, and returns (enclosing_namespaces,
  reorder_span).

  Arguments:
    file_lines: an array of LineInfo objects with .type and
       .reorder_span filled in.

  Returns:
    (None, None) if we could not find the first namespace-level
    reorder-span, or (enclosing_namespaces, reorder_span), where
    enclosing_namespaces is a string that looks like (for instance)
    'namespace ns1 { namespace ns2 {', and reorder-span is a
    [start_line, end_line) pair.
  """
  simple_namespace_re = re.compile('^\s*namespace\s+([^{]+)\s*\{\s*(//.*)?$')
  namespace_prefix = ''

  for line_number in xrange(len(file_lines)):
    line_info = file_lines[line_number]
    if line_info.deleted:
      continue

    # If we're an empty line, just ignore us.  Likewise with #include
    # lines, which aren't 'contentful' for our purposes.
    if line_info.type in (_COMMENT_LINE_RE, _BLANK_LINE_RE, _INCLUDE_RE):
      continue

    # If we're a 'contentful' line such as an #ifdef, bail.  The one
    # exception is the ifdef header-guard.
    elif line_info.type == _IFDEF_RE:
      # Ignore header-guard #ifdef.
      if (line_number < len(file_lines) - 1 and
          _IsHeaderGuard(file_lines[line_number], file_lines[line_number+1])):
        pass
      else:
        return (None, None)

    elif line_info.type in (_NAMESPACE_END_RE, _ELSE_RE, _ENDIF_RE, None):
      # Other contentful lines that cause us to bail.
      # TODO(csilvers): we could probably keep going if there are no
      # braces on the line.  We could also keep track of our #ifdef
      # depth instead of bailing on #else and #endif, and only accept
      # the fwd-decl-inside-namespace if it's at ifdef-depth 0.
      return (None, None)

    elif line_info.type == _NAMESPACE_START_RE:
      # Only handle the simple case of 'namespace <foo> {'
      m = simple_namespace_re.match(line_info.line)
      if not m:
        return (None, None)
      namespace_prefix += ('namespace %s { ' % m.group(1).strip())

    elif line_info.type == _FORWARD_DECLARE_RE:
      # If we're not in a namespace, keep going.  Otherwise, this is
      # just the situation we're looking for!
      if namespace_prefix:
        return (namespace_prefix, line_info.reorder_span)

    else:
      # We should have handled all the cases above!
      assert False, ('unknown line-info type', line_info.type)

  # If we get here, we never saw a forward-declare inside a namespace
  # (nor anything that would have caused us to bail earlier).  Alas.
  return (None, None)


# These are potential 'kind' arguments to _FirstReorderSpanWith.
# We also sort our output in this order, to the extent possible.
_MAIN_CU_INCLUDE_KIND = 1         # e.g. #include "foo.h" when editing foo.cc
_C_SYSTEM_INCLUDE_KIND = 2        # e.g. #include <stdio.h>
_CXX_SYSTEM_INCLUDE_KIND = 3      # e.g. #include <vector>
_NONSYSTEM_INCLUDE_KIND = 4       # e.g. #include "bar.h"
_PROJECT_INCLUDE_KIND = 5         # e.g. #include "myproject/quux.h"
_FORWARD_DECLARE_KIND = 6         # e.g. class Baz;
_EOF_KIND = 7                     # used at eof


def _IsSystemInclude(line_info):
  """Given a line-info, return true iff the line is a <>-style #include."""
  # The key for #includes includes the <> or "", so this is easy. :-)
  return line_info.type == _INCLUDE_RE and line_info.key[0] == '<'


def _IsMainCUInclude(line_info, filename):
  """Given a line-info, return true iff the line is a 'main-CU' #include line.

  A 'main-CU' #include line is one that is related to the file being edited.
  For instance, if we are editing foo.cc, foo.h is a main-CU #include, as
  is foo-inl.h.  The same holds if we are editing foo_test.cc.

  Arguments:
    line_info: a LineInfo structure with .type and .key filled in.
    filename: the name of the file being edited.

  Returns:
    True if line_info is an #include of a main_CU file, False else.
  """
  if line_info.type != _INCLUDE_RE or _IsSystemInclude(line_info):
    return False
  # First, normalize the filenames by getting rid of -inl.h and .h
  # suffixes (for the #include) and _test.cc and .cc extensions (for
  # the filename).  We also get rid of the "'s around the #include line.
  canonical_include = re.sub(r'(-inl\.h|\.h)$',
                             '', line_info.key.replace('"', ''))
  canonical_file = re.sub(r'(_unittest\.cc|_regtest\.cc|_test\.cc|\.cc|\.c)$',
                          '', filename)
  # .h files in /public/ match .cc files in /internal/.
  canonical_include2 = re.sub(r'/public/', '/internal/', canonical_include)
  return canonical_file in (canonical_include, canonical_include2)


def _IsSameProject(line_info, edited_file, project):
  """Return true if included file and edited file are in the same project.

  An included_file is in project 'project' if the project is a prefix of the
  included_file.  'project' should end with /.

  As a special case, if project is '<tld>', then the project is defined to
  be the top-level directory of edited_file.

  Arguments:
    line_info: a LineInfo structure with .key containing the file that is
      being included.
    edited_file: the name of the file being edited.
    project: if '<tld>', set the project path to be the top-level directory
      name of the file being edited.  If not '<tld>', this value is used to
      specify the project directory.

  Returns:
    True if line_info and filename belong in the same project, False otherwise.
  """
  included_file = line_info.key[1:]
  if project != '<tld>':
    return included_file.startswith(project)
  included_root = included_file.find(os.path.sep)
  edited_root = edited_file.find(os.path.sep)
  return (included_root > -1 and edited_root > -1 and
          included_file[0:included_root] == edited_file[0:edited_root])


def _GetLineKind(file_line, filename, separate_project_includes):
  """Given a file_line + file being edited, return best *_KIND value or None."""
  line_without_coments = _COMMENT_RE.sub('', file_line.line)
  if file_line.deleted:
    return None
  elif _IsMainCUInclude(file_line, filename):
    return _MAIN_CU_INCLUDE_KIND
  elif _IsSystemInclude(file_line) and '.' in line_without_coments:
    return _C_SYSTEM_INCLUDE_KIND
  elif _IsSystemInclude(file_line):
    return _CXX_SYSTEM_INCLUDE_KIND
  elif file_line.type == _INCLUDE_RE:
    if (separate_project_includes and
        _IsSameProject(file_line, filename, separate_project_includes)):
      return _PROJECT_INCLUDE_KIND
    return _NONSYSTEM_INCLUDE_KIND
  elif file_line.type == _FORWARD_DECLARE_RE:
    return _FORWARD_DECLARE_KIND
  else:
    return None


def _FirstReorderSpanWith(file_lines, good_reorder_spans, kind, filename,
                          flags):
  """Returns [start_line,end_line) of 1st reorder_span with a line of kind kind.

  This function iterates over all the reorder_spans in file_lines, and
  calculates the first one that has a line of the given kind in it.
  If no such reorder span is found, it takes the last span of 'lower'
  kinds (main-cu kind is lowest, forward-declare is highest).  If no
  such reorder span is found, it takes the first span of 'higher'
  kind, but not considering the forward-declare kind (we don't want to
  put an #include with the first forward-declare, because it may be
  inside a class or something weird).  If there's *still* no match, we
  return the first line past leading comments, whitespace, and #ifdef
  guard lines.  If there's *still* no match, we just insert at
  end-of-file.

  As a special case, we never return a span for forward-declares that is
  after 'contentful' code, even if other forward-declares are there.
  For instance:
     using Foo::Bar;
     class Bang;
  We want to make sure to put 'namespace Foo { class Bar; }'
  *before* the using line!

  kind is one of the following enums, with examples:
     _MAIN_CU_INCLUDE_KIND:    #include "foo.h" when editing foo.cc
     _C_SYSTEM_INCLUDE_KIND:   #include <stdio.h>
     _CXX_SYSTEM_INCLUDE_KIND: #include <vector>
     _NONSYSTEM_INCLUDE_KIND:  #include "bar.h"
     _PROJECT_INCLUDE_KIND:    #include "myproject/quux.h"
     _FORWARD_DECLARE_KIND:    class Baz;

  Arguments:
    file_lines: an array of LineInfo objects with .type and
       .reorder_span filled in.
    good_reorder_spans: a sorted list of reorder_spans to consider
       (should not include reorder_spans inside #ifdefs or
       namespaces).
    kind: one of *_KIND values.
    filename: the name of the file that file_lines comes from.
       This is passed to _GetLineKind (are we a main-CU #include?)
    flags: commandline flags, as parsed by optparse.  We use
       flags.separate_project_includes to sort the #includes for the
       current project separately from other #includes.

  Returns:
    A pair of line numbers, [start_line, end_line), that is the 'best'
    reorder_span in file_lines for the given kind.
  """
  assert kind in (_MAIN_CU_INCLUDE_KIND, _C_SYSTEM_INCLUDE_KIND,
                  _CXX_SYSTEM_INCLUDE_KIND, _NONSYSTEM_INCLUDE_KIND,
                  _PROJECT_INCLUDE_KIND, _FORWARD_DECLARE_KIND), kind
  # Figure out where the first 'contentful' line is (after the first
  # 'good' span, so we skip past header guards and the like).  Basically,
  # the first contentful line is a line not in any reorder span.
  for i in xrange(len(good_reorder_spans) - 1):
    if good_reorder_spans[i][1] != good_reorder_spans[i+1][0]:
      first_contentful_line = good_reorder_spans[i][1]
      break
  else:     # got to the end of the file without finding a break in the spans
    if good_reorder_spans:
      first_contentful_line = good_reorder_spans[-1][1]
    else:
      first_contentful_line = 0

  # Let's just find the first and last span for each kind.
  first_reorder_spans = {}
  last_reorder_spans = {}
  for reorder_span in good_reorder_spans:
    for line_number in apply(xrange, reorder_span):
      line_kind = _GetLineKind(file_lines[line_number], filename,
                               flags.separate_project_includes)
      # Ignore forward-declares that come after 'contentful' code; we
      # never want to insert new forward-declares there.
      if (line_kind == _FORWARD_DECLARE_KIND and
          line_number > first_contentful_line):
        continue
      if line_kind is not None:
        first_reorder_spans.setdefault(line_kind, reorder_span)
        last_reorder_spans[line_kind] = reorder_span

  # Find the first span of our kind.
  if kind in first_reorder_spans:
    return first_reorder_spans[kind]

  # Second choice: last span of the kinds above us:
  for backup_kind in xrange(kind - 1, _MAIN_CU_INCLUDE_KIND - 1, -1):
    if backup_kind in last_reorder_spans:
      return last_reorder_spans[backup_kind]

  # Third choice: first span of the kinds below us, but not counting
  # _FORWARD_DECLARE_KIND.
  for backup_kind in xrange(kind + 1, _FORWARD_DECLARE_KIND):
    if backup_kind in first_reorder_spans:
      return first_reorder_spans[backup_kind]

  # There are no reorder-spans at all, or they are only
  # _FORWARD_DECLARE spans.  Return the first line past the leading
  # comments, whitespace, and #ifdef guard lines, or the beginning
  # of the _FORWARD_DECLARE span, whichever is smaller.
  line_number = 0
  while line_number < len(file_lines) - 1:
    if file_lines[line_number].deleted:
      line_number += 1
    elif _IsHeaderGuard(file_lines[line_number], file_lines[line_number + 1]):
      line_number += 2    # skip over the header guard
    elif file_lines[line_number].type in (_COMMENT_LINE_RE, _BLANK_LINE_RE):
      line_number += 1
    else:
      # If the "first line" we would return is inside the forward-declare
      # reorder span, just return that span, rather than creating a new
      # span inside the existing one.
      if first_reorder_spans:
        assert first_reorder_spans.keys() == [_FORWARD_DECLARE_KIND], \
            first_reorder_spans
        if line_number >= first_reorder_spans[_FORWARD_DECLARE_KIND][0]:
          return first_reorder_spans[_FORWARD_DECLARE_KIND]
      return (line_number, line_number)

  # OK, I guess just insert at the end of the file
  return (len(file_lines), len(file_lines))


def _RemoveNamespacePrefix(fwd_decl_iwyu_line, namespace_prefix):
  """Return a version of the input line with namespace_prefix removed, or None.

  If fwd_decl_iwyu_line is
     namespace ns1 { namespace ns2 { namespace ns3 { foo } } }
  and namespace_prefix = 'namespace ns1 { namespace ns2 {', then
  this function returns 'namespace ns3 { foo }'.  It removes the
  namespace_prefix, and any } }'s at the end of the line.  If line
  does not fit this form, then this function returns None.

  Arguments:
    line: a line from iwyu about a forward-declare line to add
    namespace_prefix: a non-empty string of the form
      namespace <ns1> { namespace <ns2> { [...]

  Returns:
    A version of the input line with the namespaces in namespace
    prefix removed, or None if this is not possible because the input
    line is not of the right form.
  """
  assert namespace_prefix, "_RemoveNamespaces requires a non-empty prefix"
  if not fwd_decl_iwyu_line.startswith(namespace_prefix):
    return None

  # Remove the prefix
  fwd_decl_iwyu_line = fwd_decl_iwyu_line[len(namespace_prefix):].lstrip()

  # Remove the matching trailing }'s, preserving comments.
  num_braces = namespace_prefix.count('{')
  ending_braces_re = re.compile('(\s*\}){%d}\s*$' % num_braces)
  m = ending_braces_re.search(fwd_decl_iwyu_line)
  if not m:
    return None
  fwd_decl_iwyu_line = fwd_decl_iwyu_line[:m.start(0)]

  return fwd_decl_iwyu_line


def _DecoratedMoveSpanLines(iwyu_record, file_lines, move_span_lines, flags):
  """Given a span of lines from file_lines, returns a "decorated" result.

  First, we construct the actual contents of the move-span, as a list
  of strings (one per line).  If we see an #include in the move_span,
  we replace its comments with the ones in iwyu_record, if present
  (iwyu_record will never have any comments if flags.comments is
  False).

  Second, we construct a string, of the 'contentful' part of the
  move_span -- that is, without the leading comments -- with
  whitespace removed, and a few other changes made.  This is used for
  sorting (we remove whitespace so '# include <foo>' compares properly
  against '#include <bar>').

  Third, we figure out the 'kind' of this span: system include,
  main-cu include, etc.

  We return all of these together in a tuple, along with the
  reorder-span this move span is inside.  We pick the best
  reorder-span if one isn't already present (because it's an
  #include we're adding in, for instance.)  This allows us to sort
  all the moveable content.

  Arguments:
    iwyu_record: the IWYUOutputRecord struct for this source file.
    file_lines: a list of LineInfo objects holding the parsed output of
      the file in iwyu_record.filename
    move_span_lines: A list of LineInfo objects.  For #includes and
      forward-declares already in the file, this will be a sub-list
      of file_lines.  For #includes and forward-declares we're adding
      in, it will be a newly created list.
    flags: commandline flags, as parsed by optparse.  We use
      flags.separate_project_includes to sort the #includes for the
      current project separately from other #includes.

  Returns:
    A tuple (reorder_span, kind, sort_key, all_lines_as_list)
    sort_key is the 'contentful' part of the move_span, which whitespace
      removed, and -inl.h changed to _inl.h (so it sorts later).
    all_lines_as_list is a list of strings, not of LineInfo objects.
    Returns None if the move-span has been deleted, or for some other
      reason lacks an #include or forward-declare line.
  """
  # Get to the first contentful line.
  for i in xrange(len(move_span_lines)):
    if (not move_span_lines[i].deleted and
        move_span_lines[i].type in (_INCLUDE_RE, _FORWARD_DECLARE_RE)):
      first_contentful_line = i
      break
  else:       # for/else
    # No include or forward-declare line seen, must be a deleted span.
    return None

  firstline = move_span_lines[first_contentful_line]
  m = _INCLUDE_RE.match(firstline.line)
  if m:
    # If we're an #include, the contentful lines are easy.  But we have
    # to do the comment-replacing first.
    sort_key = firstline.line
    iwyu_version = iwyu_record.full_include_lines.get(m.group(1), '')
    if _COMMENT_LINE_RE.search(iwyu_version):  # the iwyu version has comments
      sort_key = iwyu_version                  # replace the comments
    all_lines = ([li.line for li in move_span_lines[:-1] if not li.deleted] +
                 [sort_key])
  else:
    # We're a forward-declare.  Also easy.
    contentful_list = [li.line for li in move_span_lines[first_contentful_line:]
                       if not li.deleted]
    sort_key = ''.join(contentful_list)
    all_lines = [li.line for li in move_span_lines if not li.deleted]

  # Get rid of whitespace in the contentful_lines
  sort_key = re.sub('\s+', '', sort_key)
  # Replace -inl.h with _inl.h so foo-inl.h sorts after foo.h in #includes.
  sort_key = sort_key.replace('-inl.h', '_inl.h')

  # Next figure out the kind.
  kind = _GetLineKind(firstline, iwyu_record.filename,
                      flags.separate_project_includes)

  # All we're left with is the reorder-span we're in.  Hopefully it's easy.
  reorder_span = firstline.reorder_span
  if reorder_span is None:     # must be a new #include we're adding
    # If we're a forward-declare inside a namespace, see if there's a
    # reorder span inside the same namespace we can fit into.
    if kind == _FORWARD_DECLARE_KIND:
      (namespace_prefix, possible_reorder_span) = \
          _GetFirstNamespaceLevelReorderSpan(file_lines)
      if (namespace_prefix and possible_reorder_span and
          firstline.line.startswith(namespace_prefix)):
        # Great, we can go into this reorder_span.  We also need to
        # modify all-lines because this line doesn't need the
        # namespace prefix anymore.  Make sure we can do that before
        # succeeding.
        new_firstline = _RemoveNamespacePrefix(firstline.line, namespace_prefix)
        if new_firstline:
          assert all_lines[first_contentful_line] == firstline.line
          all_lines[first_contentful_line] = new_firstline
          reorder_span = possible_reorder_span

    # If that didn't work out, find a top-level reorder span to go into.
    if reorder_span is None:
      # TODO(csilvers): could make this more efficient by storing, per-kind.
      toplevel_reorder_spans = _GetToplevelReorderSpans(file_lines)
      reorder_span = _FirstReorderSpanWith(file_lines, toplevel_reorder_spans,
                                           kind, iwyu_record.filename, flags)

  return (reorder_span, kind, sort_key, all_lines)


def _CommonPrefixLength(a, b):
  """Given two lists, returns the index of 1st element not common to both."""
  end = min(len(a), len(b))
  for i in xrange(end):
    if a[i] != b[i]:
      return i
  return end


def _NormalizeNamespaceForwardDeclareLines(lines):
  """'Normalize' namespace lines in a list of output lines and return new list.

  When suggesting new forward-declares to insert, iwyu uses the following
  format, putting each class on its own line with all namespaces:
     namespace foo { namespace bar { class A; } }
     namespace foo { namespace bar { class B; } }
     namespace foo { namespace bang { class C; } }
  We convert this to 'normalized' form, which puts namespaces on their
  own line and collects classes together:
     namespace foo {
     namespace bar {
     class A;
     class B;
     }
     namespace bang {
     class C;
     }
     }

  Non-namespace lines are left alone.  Only adjacent namespace lines
  from the input are merged.

  Arguments:
    lines: a list of output-lines -- that is, lines that are ready to
       be emitted as-is to the output file.

  Returns:
    A new version of lines, with namespace lines normalized as above.
  """
  # iwyu input is very regular, which is nice.
  iwyu_namespace_re = re.compile('namespace ([^{]*) { ')
  iwyu_classname_re = re.compile('{ ([^{}]*) }')

  retval = []
  current_namespaces = []
  # We append a blank line so the final namespace-closing happens "organically".
  for line in lines + ['']:
    namespaces_in_line = iwyu_namespace_re.findall(line)
    differ_pos = _CommonPrefixLength(namespaces_in_line, current_namespaces)
    namespaces_to_close = current_namespaces[differ_pos:]
    namespaces_to_open = namespaces_in_line[differ_pos:]
    retval.extend(['}'] * len(namespaces_to_close))
    retval.extend('namespace %s {' % ns for ns in namespaces_to_open)
    current_namespaces = namespaces_in_line
    # Now add the current line.  If we were a namespace line, it's the
    # 'class' part of the line (everything but the 'namespace {'s).
    if namespaces_in_line:
      m = iwyu_classname_re.search(line)
      if not m:
        raise FixIncludesError('Malformed namespace line from iwyu: %s', line)
      retval.append(m.group(1))
    else:
      retval.append(line)

  assert retval and retval[-1] == '', 'What happened to our sentinel line?'
  return retval[:-1]


def _DeleteLinesAccordingToIwyu(iwyu_record, file_lines):
  """Deletes all lines that iwyu_record tells us to, and cleans up after."""
  for line_number in iwyu_record.lines_to_delete:
    # Delete the entire move-span (us and our preceding comments).
    for i in apply(xrange, file_lines[line_number].move_span):
      file_lines[i].deleted = True

  while True:
    num_deletes = _DeleteEmptyNamespaces(file_lines)
    num_deletes += _DeleteEmptyIfdefs(file_lines)
    if num_deletes == 0:
      break

  # Also delete any duplicate lines in the input.  To avoid trouble
  # (accidentally deleting inside an #ifdef, for instance), we only
  # check 'top-level' #includes and forward-declares.
  toplevel_reorder_spans = _GetToplevelReorderSpans(file_lines)
  _DeleteDuplicateLines(file_lines, toplevel_reorder_spans)

  # If a whole reorder span was deleted, check if it has extra
  # whitespace on both sides that we could trim.  We've already
  # deleted extra blank lines inside #ifdefs and namespaces,
  # so looking at toplevel spans is enough.
  for reorder_span in toplevel_reorder_spans:
    _DeleteExtraneousBlankLines(file_lines, reorder_span)


def _MarkUnnecessaryLinesAccordingToIwyu(iwyu_record, file_lines):
  """The equivalent of _DeleteLinesAccordingToIwyu, but used in --safe mode.

  When the user runs with --safe, we do not delete lines that iwyu tells
  us to delete.  Instead we add a comment to the line saying that it's
  been judged unnecessary according to iwyu.  This will allow for easy
  cleanup later, and is also self-documenting.

  Arguments:
    iwyu_record: the IWYUOutputRecord struct for this source file
    file_lines: a list of LineInfo objects holding the parsed output of
      the file in iwyu_record.filename
  """
  for line_number in iwyu_record.lines_to_delete:
    if '//' in file_lines[line_number].line:   # line already has a comment
      file_lines[line_number].line += '; '
    else:
      file_lines[line_number].line += '  // '
    file_lines[line_number].line += 'iwyu says this can be removed'


def FixFileLines(iwyu_record, file_lines, flags):
  """Applies one block of lines from the iwyu output script.

  Called once we have read all the lines from the iwyu output script
  pertaining to a single source file, and parsed them into an
  iwyu_record.  At that point we edit the source file, remove the old
  #includes and forward-declares, insert the #includes and
  forward-declares, and reorder the lot, all as specified by the iwyu
  output script.  The resulting source code lines are returned.

  Arguments:
    iwyu_record: an IWYUOutputRecord object holding the parsed output
      of the include-what-you-use script (run at verbose level 1 or
      higher) pertaining to a single source file.
    file_lines: a list of LineInfo objects holding the parsed output of
      the file in iwyu_record.filename
    flags: commandline flags, as parsed by optparse.  We use
       flags.safe to turn off deleting lines, and use the other
       flags indirectly (via calls to other routines).

  Returns:
    An array of 'fixed' source code lines, after modifications as
    specified by iwyu.
  """
  # First delete the includes and forward-declares that we should delete.
  # This is easy since iwyu tells us the line numbers.
  if flags.safe:
    _MarkUnnecessaryLinesAccordingToIwyu(iwyu_record, file_lines)
  else:
    _DeleteLinesAccordingToIwyu(iwyu_record, file_lines)

  # With these deletions, we may be able to merge together some
  # reorder-spans.  Recalculate them to see.
  _CalculateReorderSpans(file_lines)

  # For every move-span in our file -- that's every #include and
  # forward-declare we saw -- 'decorate' the move-range to allow us
  # to sort them.
  move_spans = set([fl.move_span for fl in file_lines if fl.move_span])
  decorated_move_spans = []
  for (start_line, end_line) in move_spans:
    decorated_span = _DecoratedMoveSpanLines(iwyu_record, file_lines,
                                             file_lines[start_line:end_line],
                                             flags)
    if decorated_span:
      decorated_move_spans.append(decorated_span)

  # Now let's add in a decorated move-span for all the new #includes
  # and forward-declares.
  for line in iwyu_record.includes_and_forward_declares_to_add:
    line_info = LineInfo(line)
    m = _INCLUDE_RE.match(line)
    if m:
      line_info.type = _INCLUDE_RE
      line_info.key = m.group(1)
    else:
      line_info.type = _FORWARD_DECLARE_RE
    decorated_span = _DecoratedMoveSpanLines(iwyu_record, file_lines,
                                             [line_info], flags)
    assert decorated_span, 'line to add is not an #include or fwd-decl?'
    decorated_move_spans.append(decorated_span)

  # Add a sentinel decorated move-span, to make life easy, and sort.
  decorated_move_spans.append(((len(file_lines), len(file_lines)),
                               _EOF_KIND, '', []))
  decorated_move_spans.sort()

  # Now go through all the lines of the input file and construct the
  # output file.  Before we get to the next reorder-span, we just
  # copy lines over verbatim (ignoring deleted lines, of course).
  # In a reorder-span, we just print the sorted content, introducing
  # blank lines when appropriate.
  output_lines = []
  line_number = 0
  while line_number < len(file_lines):
    current_reorder_span = decorated_move_spans[0][0]

    # Just copy over all the lines until the next reorder span.
    while line_number < current_reorder_span[0]:
      if not file_lines[line_number].deleted:
        output_lines.append(file_lines[line_number].line)
      line_number += 1

    # Now fill in the contents of the reorder-span from decorated_move_spans
    new_lines = []
    while (decorated_move_spans and
           decorated_move_spans[0][0] == current_reorder_span):
      new_lines.extend(decorated_move_spans[0][3])   # the full content
      if (len(decorated_move_spans) > 1 and
          _ShouldInsertBlankLine(decorated_move_spans[0],
                                 decorated_move_spans[1], file_lines, flags)):
        new_lines.append('')
      decorated_move_spans = decorated_move_spans[1:]   # pop
    # Now do the munging to convert namespace lines from the iwyu input
    # format to the 'official style' format:
    #    'namespace foo { class Bar; }\n' -> 'namespace foo {\nclass Bar;\n}'
    # along with collecting multiple classes in the same namespace.
    new_lines = _NormalizeNamespaceForwardDeclareLines(new_lines)
    output_lines.extend(new_lines)
    line_number = current_reorder_span[1]               # go to end of span

  return output_lines


def FixOneFile(iwyu_record, flags):
  """Fix the #include and forward-declare lines of one file.

  Given an iwyu record for a single file, listing the #includes and
  forward-declares to add, remove, and re-sort, loads the file,
  makes the fixes, and writes the file out again.  The flags
  effect how file loading and saving are done, as well as the
  details of the fixing.

  Arguments:
    iwyu_record: an IWYUOutputRecord object holding the parsed output
      of the include-what-you-use script (run at verbose level 1 or
      higher) pertaining to a single source file.
      iwyu_record.filename indicates what file to edit.
    flags: commandline flags, as parsed by optparse.  We use
       flags.dry_run, which determines whether we write the 'fixed'
       file to disk, flags.checkout_command, which determines
       how we handle files that are not writable, and other flags
       indirectly.

  Returns:
    1 if the file needed to be fixed, or 0 if it was already all ok.
  """
  file_contents = _ReadWriteableFile(iwyu_record.filename,
                                     flags.dry_run or flags.checkout_command)
  if not file_contents:
    print '(skipping %s: not a writable file)' % iwyu_record.filename
    return 0
  print ">>> Fixing #includes in '%s'" % iwyu_record.filename
  file_lines = ParseOneFile(file_contents, iwyu_record)
  old_lines = [fl.line for fl in file_lines
               if fl is not None and fl.line is not None]
  fixed_lines = FixFileLines(iwyu_record, file_lines, flags)
  fixed_lines = [line for line in fixed_lines if line is not None]
  if old_lines == fixed_lines:
    print "No changes in file", iwyu_record.filename
    return 0

  if flags.dry_run:
    PrintFileDiff(old_lines, fixed_lines)
  else:
    _WriteFileContentsIfPossible(iwyu_record.filename, fixed_lines,
                                 flags.checkout_command)
  return 1


def ProcessIWYUOutput(f, files_to_process, flags):
  """Fix the #include and forward-declare lines as directed by f.

  Given a file object that has the output of the include_what_you_use
  script, see every file to be edited and edit it, if appropriate.

  Arguments:
    f: an iterable object that is the output of include_what_you_use.
    files_to_process: A set of filenames, or None.  If not None, we
       ignore files mentioned in f that are not in files_to_process.
    flags: commandline flags, as parsed by optparse.  We do not use
       any flags directly, but pass them to other routines.

  Returns:
    The number of files that had to be modified (because they weren't
    already all correct).  In dry_run mode, returns the number of
    files that would have been modified.
  """
  # First collect all the iwyu data from stdin.
  iwyu_output_records = {}    # key is filename, value is an IWYUOutputRecord
  while True:
    iwyu_output_parser = IWYUOutputParser()
    try:
      iwyu_record = iwyu_output_parser.ParseOneRecord(f, flags)
      if not iwyu_record:
        break
    except FixIncludesError, why:
      print 'ERROR: %s' % why
      continue
    filename = iwyu_record.filename
    if files_to_process is not None and filename not in files_to_process:
      print '(skipping %s: not listed on commandline)' % filename
      continue

    if filename in iwyu_output_records:
      iwyu_output_records[filename].Merge(iwyu_record)
    else:
      iwyu_output_records[filename] = iwyu_record

  # Now do all the fixing.
  num_fixes_made = 0
  for iwyu_record in iwyu_output_records.itervalues():
    try:
      num_fixes_made += FixOneFile(iwyu_record, flags)
    except FixIncludesError, why:
      print 'ERROR: %s - skipping file %s' % (why, iwyu_record.filename)
  return num_fixes_made


def SortIncludesInFiles(files_to_process, flags):
  """For each file in files_to_process, sort its #includes.

  This reads each input file, sorts the #include lines, and replaces
  the input file with the result.  Like ProcessIWYUOutput, this
  requires that the file be writable, or that flags.checkout_command
  be set.  SortIncludesInFiles does not add or remove any #includes.
  It also ignores forward-declares.

  Arguments:
    files_to_process: a list (or set) of filenames.
    flags: commandline flags, as parsed by optparse.  We do not use
       any flags directly, but pass them to other routines.

  Returns:
    The number of files that had to be modified (because they weren't
    already all correct, that is, already in sorted order).
  """
  num_fixes_made = 0
  for filename in files_to_process:
    # An empty iwyu record has no adds or deletes, so its only effect
    # is to cause us to sort the #include lines.  (Since fix_includes
    # gets all its knowledge of where forward-declare lines are from
    # the iwyu input, with an empty iwyu record it just ignores all
    # the forward-declare lines entirely.)
    sort_only_iwyu_record = IWYUOutputRecord(filename)
    num_fixes_made += FixOneFile(sort_only_iwyu_record, flags)
  return num_fixes_made


def main(argv):
  # Parse the command line.
  parser = optparse.OptionParser(usage=_USAGE)
  parser.add_option('-b', '--blank_lines', action='store_true', default=True,
                    help=('Put a blank line between primary header file and'
                          ' C/C++ system #includes, and another blank line'
                          ' between system #includes and google #includes'
                          ' [default]'))
  parser.add_option('--noblank_lines', action='store_false', dest='blank_lines')

  parser.add_option('--comments', action='store_true', default=True,
                    help='Put comments after the #include lines [default]')
  parser.add_option('--nocomments', action='store_false', dest='comments')

  parser.add_option('--safe', action='store_true', default=False,
                    help='Do not remove #includes/fwd-declares, just add them')

  parser.add_option('-s', '--sort_only', action='store_true',
                    help=('Just sort #includes of files listed on cmdline;'
                          ' do not add or remove any #includes'))

  parser.add_option('-n', '--dry_run', action='store_true', default=False,
                    help=('Do not actually edit any files; just print diffs.'
                          ' Return code is 0 if no changes are needed,'
                          ' else min(the number of files that would be'
                          ' modified, 100)'))

  parser.add_option('--checkout_command', default='',
                    help=('A command, such as "p4 edit", to run on each'
                          ' non-writeable file before modifying it.  The name'
                          ' of the file will be appended to the command after'
                          ' a space.  The command will not be run on any file'
                          ' that does not need to change.'))

  parser.add_option('--separate_project_includes',
                    help=('Sort #includes for current project separately'
                          ' from all other #includes.  This flag specifies'
                          ' the root directory of the current project.'
                          ' If the value is "<tld>", #includes that share the'
                          ' same top-level directory are assumed to be in the'
                          ' same project.  If not specified, project #includes'
                          ' will be sorted with other non-system #includes.'))

  (flags, files_to_modify) = parser.parse_args(argv[1:])
  if files_to_modify:
    files_to_modify = set(files_to_modify)
  else:
    files_to_modify = None

  if (flags.separate_project_includes and
      not flags.separate_project_includes.startswith('<') and  # 'special' vals
      not flags.separate_project_includes.endswith(os.path.sep)):
    flags.separate_project_includes += os.path.sep

  if flags.sort_only:
    if not files_to_modify:
      sys.exit('FATAL ERROR: -s flag requires a list of filenames')
    return SortIncludesInFiles(files_to_modify, flags)
  else:
    return ProcessIWYUOutput(sys.stdin, files_to_modify, flags)


if __name__ == '__main__':
  num_files_fixed = main(sys.argv)
  sys.exit(min(num_files_fixed, 100))