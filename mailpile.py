#!/usr/bin/python
ABOUT="""\
Mailpile.py - a tool for searching and      Copyright 2011, Bjarni R. Einarsson
             organizing piles of e-mail                <http://bre.klaki.net/>

This program is free software: you can redistribute it and/or modify it under
the terms of the  GNU  Affero General Public License as published by the Free
Software Foundation, either version 3 of the License, or (at your option) any
later version.
"""
###############################################################################
import codecs, datetime, email.parser, getopt, hashlib, locale, mailbox
import os, cPickle, random, re, rfc822, struct, sys, time
import lxml.html


global APPEND_FD_CACHE, APPEND_FD_CACHE_ORDER, APPEND_FD_CACHE_SIZE
global WORD_REGEXP, STOPLIST

WORD_REGEXP = re.compile('[^\s!@#$%^&*\(\)_+=\{\}\[\]:\"|;\'\\\<\>\?,\.\/\-]{2,}')
# FIXME: This stoplist may be a bad idea.
STOPLIST = ('an', 'and', 'are', 'as', 'at', 'by', 'for', 'from', 'has', 'in',
            'is', 'og', 'or', 're', 'so', 'the', 'to', 'was')


def b64c(b):
  return b.replace('\n', '').replace('=', '').replace('/', '_')

def sha1b64(s):
  h = hashlib.sha1()
  h.update(s.encode('utf-8'))
  return h.digest().encode('base64')

def strhash(s, length):
  s2 = re.sub('[^0123456789abcdefghijklmnopqrstuvwxyz]+', '', s.lower())
  while len(s2) < length:
    s2 += b64c(sha1b64(s)).lower()
  return s2[:length]

def b36(number):
  alphabet = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ'
  base36 = ''
  while number:
    number, i = divmod(number, 36)
    base36 = alphabet[i] + base36
  return base36 or alphabet[0]


# Indexing messages is an append-heavy operation, and some files are
# appended to much more often than others.  This implements a simple
# LRU cache of file descriptors we are appending to.
APPEND_FD_CACHE = {}
APPEND_FD_CACHE_SIZE = 500
APPEND_FD_CACHE_ORDER = []
def flush_append_cache(ratio=1, count=None):
  global APPEND_FD_CACHE, APPEND_FD_CACHE_ORDER
  drop = count or ratio*len(APPEND_FD_CACHE)
  for fn in APPEND_FD_CACHE_ORDER[:drop]:
    APPEND_FD_CACHE[fn].close()
    del APPEND_FD_CACHE[fn]
  APPEND_FD_CACHE_ORDER[:drop] = []

def cached_open(filename, mode):
  global APPEND_FD_CACHE, APPEND_FD_CACHE_ORDER, APPEND_FD_CACHE_SIZE
  if mode == 'a':
    if filename not in APPEND_FD_CACHE:
      if len(APPEND_FD_CACHE) > APPEND_FD_CACHE_SIZE:
        flush_append_cache(count=1)
      try:
        APPEND_FD_CACHE[filename] = open(filename, 'a')
      except:
        # Too many open files?  Close a bunch and try again.
        flush_append_cache(ratio=0.3)
        APPEND_FD_CACHE[filename] = open(filename, 'a')
      APPEND_FD_CACHE_ORDER.append(filename)
    else:
      APPEND_FD_CACHE_ORDER.remove(filename)
      APPEND_FD_CACHE_ORDER.append(filename)
    return APPEND_FD_CACHE[filename]
  else:
    if filename in APPEND_FD_CACHE:
      APPEND_FD_CACHE[filename].close()
      APPEND_FD_CACHE_ORDER.remove(filename)
      del APPEND_FD_CACHE[filename]
    return open(filename, mode)


##[ Enhanced mailbox classes for incremental updates ]#########################

class IncrementalMbox(mailbox.mbox):

  last_parsed = 0
  save_to = None

  def __getstate__(self):
    odict = self.__dict__.copy()
    del odict['_file']
    return odict

  def __setstate__(self, dict):
    self.__dict__.update(dict)
    try:
      self._file = open(self._path, 'rb+')
    except IOError, e:
      if e.errno == errno.ENOENT:
        raise NoSuchMailboxError(self._path)
      elif e.errno == errno.EACCES:
        self._file = open(self._path, 'rb')
      else:
        raise
    self._update_toc()

  def _update_toc(self):
    self._file.seek(0, 2)
    if self._file_length == self._file.tell(): return

    self._file.seek(self._toc[self._next_key-1][0])
    line = self._file.readline()
    if not line.startswith('From '):
      raise IOError("Mailbox has been modified")

    self._file.seek(self._file_length)
    start = None
    while True:
      line_pos = self._file.tell()
      line = self._file.readline()
      if line.startswith('From '):
        if start:
          self._toc[self._next_key] = (start, line_pos - len(os.linesep))
          self._next_key += 1
        start = line_pos
      elif line == '':
        self._toc[self._next_key] = (start, line_pos)
        self._next_key += 1
        break
    self._file_length = self._file.tell()
    self.save(None)

  def save(self, session, to=None):
    if to:
      self.save_to = to
    if self.save_to and len(self) > 0:
      if session: session.ui.mark('Saving state to %s' % self.save_to)
      fd = open(self.save_to, 'w')
      cPickle.dump(self, fd)
      fd.close()

  def get_msg_ptr(self, idx, toc_id):
    return '%s%s' % (idx, b36(long(self.get_file(toc_id)._pos)))

  def get_msg_size(self, toc_id):
    return self._toc[toc_id][1] - self._toc[toc_id][0]


##[ The search and index code itself ]#########################################

class PostingList(object):

  MAX_SIZE = 60  # perftest gives: 75% below 500ms, 50% below 100ms
  HASH_LEN = 12

  @classmethod
  def Optimize(cls, session, idx, force=False):
    flush_append_cache()

    postinglist_kb = session.config.get('postinglist_kb', cls.MAX_SIZE)
    postinglist_dir = session.config.postinglist_dir()

    # Pass 1: Compact all files that are 90% or more of our target size
    for fn in sorted(os.listdir(postinglist_dir)):
      if (force or
          os.path.getsize(os.path.join(postinglist_dir, fn)) >
                                                        900*postinglist_kb):
        session.ui.mark('Pass 1: Compacting >%s<' % fn)
        # FIXME: Remove invalid and deleted messages from posting lists.
        cls(session, fn, sig=fn).save()

    # Pass 2: While mergable pair exists: merge them!
    files = [n for n in os.listdir(postinglist_dir) if len(n) > 1]
    files.sort(key=lambda a: -len(a))
    for fn in files:
      size = os.path.getsize(os.path.join(postinglist_dir, fn))
      fnp = fn[:-1]
      while not os.path.exists(os.path.join(postinglist_dir, fnp)):
        fnp = fnp[:-1]
      size += os.path.getsize(os.path.join(postinglist_dir, fnp))
      if (size < (1024*postinglist_kb-(cls.HASH_LEN*6))):
        session.ui.mark('Pass 2: Merging %s into %s' % (fn, fnp))
        fd = cached_open(os.path.join(postinglist_dir, fn), 'r')
        fdp = cached_open(os.path.join(postinglist_dir, fnp), 'a')
        try:
          for line in fd:
            fdp.write(line)
        except:
          flush_append_cache()
          raise
        finally:
          fd.close()
          os.remove(os.path.join(postinglist_dir, fn))

    flush_append_cache()
    filecount = len(os.listdir(postinglist_dir))
    session.ui.mark('Optimized %s posting lists' % filecount)
    return filecount

  @classmethod
  def Append(cls, session, word, mail_id, compact=True):
    config = session.config
    sig = cls.WordSig(word)
    fd, fn = cls.GetFile(session, sig, mode='a')
    if (compact
        and (os.path.getsize(os.path.join(config.postinglist_dir(), fn)) >
             (1024*config.get('postinglist_kb', cls.MAX_SIZE))-(cls.HASH_LEN*6))
        and (random.randint(0, 50) == 1)):
      # This will compact the files and split out hot-spots, but we only bother
      # "once in a while" when the files are "big".
      fd.close()
      pls = cls(session, word)
      pls.append(mail_id)
      pls.save()
    else:
      # Quick and dirty append is the default.
      fd.write('%s\t%s\n' % (sig, mail_id))

  @classmethod
  def WordSig(cls, word):
    return strhash(word, cls.HASH_LEN*2)

  @classmethod
  def GetFile(cls, session, sig, mode='r'):
    sig = sig[:cls.HASH_LEN]
    while len(sig) > 0:
      fn = os.path.join(session.config.postinglist_dir(), sig)
      try:
        if os.path.exists(fn): return (cached_open(fn, mode), sig)
      except:
        pass

      if len(sig) > 1:
        sig = sig[:-1]
      else:
        if 'r' in mode:
          return (None, sig)
        else:
          return (cached_open(fn, mode), sig)
    # Not reached
    return (None, None)

  def __init__(self, session, word, sig=None):
    self.config = session.config
    self.session = session
    self.sig = sig or PostingList.WordSig(word)
    self.word = word
    self.WORDS = {self.sig: set()}
    self.load()

  def load(self):
    self.size = 0
    fd, sig = PostingList.GetFile(self.session, self.sig)
    self.filename = sig
    if fd:
      for line in fd:
        self.size += len(line)
        words = line.strip().split('\t')
        if len(words) > 1:
          if words[0] not in self.WORDS: self.WORDS[words[0]] = set()
          self.WORDS[words[0]] |= set(words[1:])
      fd.close()

  def fmt_file(self, prefix):
    output = []
    self.session.ui.mark('Formatting prefix %s' % unicode(prefix))
    for word in self.WORDS:
      if word.startswith(prefix) and len(self.WORDS[word]) > 0:
        output.append('%s\t%s\n' % (word,
                               '\t'.join(['%s' % x for x in self.WORDS[word]])))
    return ''.join(output)

  def save(self, prefix=None, compact=True, mode='w'):
    prefix = prefix or self.filename
    output = self.fmt_file(prefix)
    while (compact and
           len(output) > 1024*self.config.get('postinglist_kb', self.MAX_SIZE)
           and len(prefix) < self.HASH_LEN):
      biggest = self.sig
      for word in self.WORDS:
        if len(self.WORDS[word]) > len(self.WORDS[biggest]):
          biggest = word
      if len(biggest) > len(prefix):
        biggest = biggest[:len(prefix)+1]
        self.save(prefix=biggest, mode='a')

        for key in [k for k in self.WORDS if k.startswith(biggest)]:
          del self.WORDS[key]
        output = self.fmt_file(prefix)

    try:
      outfile = os.path.join(self.config.postinglist_dir(), prefix)
      if output:
        try:
          fd = cached_open(outfile, mode)
          fd.write(output)
          return len(output)
        finally:
          if mode != 'a': fd.close()
      elif os.path.exists(outfile):
        os.remove(outfile)
    except:
      self.session.ui.warning('%s' % (sys.exc_info(), ))
    return 0

  def hits(self):
    return self.WORDS[self.sig]

  def append(self, eid):
    self.WORDS[self.sig].add(eid)
    return self

  def remove(self, eid):
    try:
      self.WORDS[self.sig].remove(eid)
    except KeyError:
      pass
    return self


class MailIndex(object):
  """This is a lazily parsing object representing a mailpile index."""

  MSG_IDX     = 0
  MSG_PTR     = 1
  MSG_SIZE    = 2
  MSG_ID      = 3
  MSG_DATE    = 4
  MSG_FROM    = 5
  MSG_SUBJECT = 6
  MSG_TAGS    = 7
  MSG_REPLIES = 8
  MSG_CONV_ID = 9

  def __init__(self, config):
    self.config = config
    self.INDEX = []
    self.PTRS = {}
    self.MSGIDS = {}
    self.CACHE = {}

  def l2m(self, line):
    return line.decode('utf-8').split(u'\t')

  def m2l(self, message):
    return (u'\t'.join([unicode(p) for p in message])).encode('utf-8')

  def load(self, session):
    self.INDEX = []
    self.PTRS = {}
    self.MSGIDS = {}
    session.ui.mark('Loading metadata index...')
    try:
      fd = open(self.config.mailindex_file(), 'r')
      for line in fd:
        line = line.strip()
        if line and not line.startswith('#'):
          self.INDEX.append(line)
      fd.close()
    except IOError:
      session.ui.warning(('Metadata index not found: %s'
                          ) % self.config.mailindex_file())
    session.ui.mark('Loaded metadata for %d messages' % len(self.INDEX))

  def save(self, session):
    session.ui.mark("Saving metadata index...")
    fd = open(self.config.mailindex_file(), 'w')
    fd.write('# This is the mailpile.py index file.\n')
    fd.write('# We have %d messages!\n' % len(self.INDEX))
    for item in self.INDEX:
      fd.write(item)
      fd.write('\n')
    fd.close()
    flush_append_cache()
    session.ui.mark("Saved metadata index")

  def update_ptrs_and_msgids(self, session):
    session.ui.mark('Updating high level indexes')
    for offset in range(0, len(self.INDEX)):
      message = self.l2m(self.INDEX[offset])
      if len(message) > self.MSG_CONV_ID:
        self.PTRS[message[self.MSG_PTR]] = offset
        self.MSGIDS[message[self.MSG_ID]] = offset
      else:
        session.ui.warning('Bogus line: %s' % line)

  def hdr(self, msg, name, value=None):
    decoded = email.header.decode_header(value or msg[name] or '')
    try:
      return (' '.join([t[0].decode(t[1] or 'iso-8859-1') for t in decoded])
              ).replace('\r', ' ').replace('\t', ' ').replace('\n', ' ')
    except:
      try:
        return (' '.join([t[0].decode(t[1] or 'utf-8') for t in decoded])
                ).replace('\r', ' ').replace('\t', ' ').replace('\n', ' ')
      except:
        session.ui.warning('Boom: %s/%s' % (msg[name], decoded))
        return ''

  def scan_mailbox(self, session, idx, mailbox_fn, mailbox_opener):
    mbox = mailbox_opener(session, idx, mailbox_fn)
    if mbox.last_parsed+1 == len(mbox): return 0

    if len(self.PTRS.keys()) == 0:
      self.update_ptrs_and_msgids(session)

    added = 0
    msg_date = int(time.time())
    for i in range(mbox.last_parsed+1, len(mbox)):
      parse_status = ('%s: Reading your mail: %d%% (%d/%d messages)'
                      ) % (idx, 100 * i/len(mbox), i, len(mbox))

      msg_ptr = mbox.get_msg_ptr(idx, i)
      if msg_ptr in self.PTRS:
        if (i % 317) == 0: session.ui.mark(parse_status)
        continue
      else:
        session.ui.mark(parse_status)

      # Message new or modified, let's parse it.
      p = email.parser.Parser()
      msg = p.parse(mbox.get_file(i))
      msg_id = b64c(sha1b64((self.hdr(msg, 'message-id') or msg_ptr).strip()))
      if msg_id in self.MSGIDS:
        msg_info = self.l2m(self.INDEX[self.MSGIDS[msg_id]])
        old_ptr = msg_info[self.MSG_PTR]
        if (old_ptr != msg_ptr) and (old_ptr[:3] == msg_ptr[:3]):
          # Just update location, if it has moved within the same mailbox - if
          # the same message is present in multiple mailboxes, just ignore it.
          msg_info[self.MSG_PTR] = msg_ptr
          msg_info[self.MSG_SIZE] = b36(mbox.get_msg_size(i))
          self.INDEX[self.MSGIDS[msg_id]] = self.m2l(msg_info)
          self.PTRS[msg_ptr] = self.MSGIDS[msg_id]
          added += 1
      else:
        # Add new message!
        msg_mid = b36(len(self.INDEX))

        try:
          msg_date = int(rfc822.mktime_tz(
                                   rfc822.parsedate_tz(self.hdr(msg, 'date'))))
        except:
          session.ui.warning('Date parsing: %s' % (sys.exc_info(), ))
          # This is a hack: We assume the messages in the mailbox are in
          # chronological order and just add 1 second to the date of the last
          # message.  This should be a better-than-nothing guess.
          msg_date += 1

        msg_conv = None
        refs = set((self.hdr(msg, 'references')+' '+self.hdr(msg, 'in-reply-to')
                    ).replace(',', ' ').strip().split())
        for ref_id in [b64c(sha1b64(r)) for r in refs]:
          try:
            # Get conversation ID ...
            ref_mid = self.MSGIDS[ref_id]
            msg_conv = self.l2m(self.INDEX[ref_mid])[self.MSG_CONV_ID]
            # Update root of conversation thread
            parent = self.l2m(self.INDEX[int(msg_conv, 36)])
            parent[self.MSG_REPLIES] += '%s,' % msg_mid
            self.INDEX[int(msg_conv, 36)] = self.m2l(parent)
            break
          except:
            pass
        if not msg_conv:
          # FIXME: If subject implies this is a reply, scan back a couple
          #        hundred messages for similar subjects - but not infinitely,
          #        conversations don't last forever.
          msg_conv = msg_mid

        keywords = self.index_message(session, msg_mid, msg, msg_date,
                                      compact=False,
                                      filter_hook=self.filter_keywords)
        tags = [k.split(':')[0] for k in keywords if k.endswith(':tag')]

        msg_info = [msg_mid,                   # Our index ID
                    msg_ptr,                   # Location on disk
                    b36(mbox.get_msg_size(i)), # Size?
                    msg_id,                    # Message-ID
                    b36(msg_date),             # Date as a UTC timestamp
                    self.hdr(msg, 'from'),     # From:
                    self.hdr(msg, 'subject'),  # Subject
                    ','.join(tags),            # Initial tags
                    '',                        # No replies for now
                    msg_conv]                  # Conversation ID

        self.PTRS[msg_ptr] = self.MSGIDS[msg_id] = len(self.INDEX)
        self.INDEX.append(self.m2l(msg_info))
        added += 1

    if added:
      mbox.last_parsed = i
      mbox.save(session)
    session.ui.mark('%s: Indexed mailbox: %s' % (idx, mailbox_fn))
    return added

  def filter_keywords(self, session, msg_mid, msg, keywords):
    keywordmap = {}
    msg_idx_list = [msg_mid]
    for kw in keywords:
      keywordmap[kw] = msg_idx_list

    for fid, terms, tags, comment in session.config.get_filters():
      if (terms == '*'
      or  len(self.search(None, terms.split(), keywords=keywordmap)) > 0):
        for t in tags.split():
          kw = '%s:tag' % t[1:]
          if t[0] == '-':
            if kw in keywordmap: del keywordmap[kw]
          else:
            keywordmap[kw] = msg_idx_list

    return set(keywordmap.keys())

  def index_message(self, session, msg_mid, msg, msg_date,
                    compact=True, filter_hook=None):
    keywords = set()
    for part in msg.walk():
      charset = part.get_charset() or 'iso-8859-1'
      if part.get_content_type() == 'text/plain':
        textpart = part.get_payload(None, True)
      elif part.get_content_type() == 'text/html':
        payload = part.get_payload(None, True).decode(charset)
        if payload:
          try:
            textpart = lxml.html.fromstring(payload).text_content()
          except:
            session.ui.warning('Parsing failed: %s' % payload)
            textpart = None
        else:
          textpart = None
      else:
        textpart = None

      att = part.get_filename()
      if att:
        keywords.add('attachment:has')
        keywords |= set([t+':att' for t in re.findall(WORD_REGEXP, att.lower())])
        textpart = (textpart or '') + ' ' + att

      if textpart:
        # FIXME: Does this lowercase non-ASCII characters correctly?
        keywords |= set(re.findall(WORD_REGEXP, textpart.lower()))

    mdate = datetime.date.fromtimestamp(msg_date)
    keywords.add('%s:year' % mdate.year)
    keywords.add('%s:month' % mdate.month)
    keywords.add('%s:day' % mdate.day)
    keywords.add('%s-%s-%s:date' % (mdate.year, mdate.month, mdate.day))

    msg_subject = self.hdr(msg, 'subject').lower()
    msg_list = self.hdr(msg, 'list-id').lower()
    msg_from = self.hdr(msg, 'from').lower()
    msg_to = self.hdr(msg, 'to').lower()

    keywords |= set(re.findall(WORD_REGEXP, msg_subject))
    keywords |= set(re.findall(WORD_REGEXP, msg_from))
    keywords |= set([t+':subject' for t in re.findall(WORD_REGEXP, msg_subject)])
    keywords |= set([t+':list' for t in re.findall(WORD_REGEXP, msg_list)])
    keywords |= set([t+':from' for t in re.findall(WORD_REGEXP, msg_from)])
    keywords |= set([t+':to' for t in re.findall(WORD_REGEXP, msg_to)])
    keywords -= set(STOPLIST)

    if filter_hook:
      keywords = filter_hook(session, msg_mid, msg, keywords)

    for word in keywords:
      try:
        PostingList.Append(session, word, msg_mid, compact=compact)
      except UnicodeDecodeError:
        # FIXME: we just ignore garbage
        pass

    return keywords

  def get_msg_by_idx(self, msg_idx):
    try:
      if msg_idx not in self.CACHE:
        self.CACHE[msg_idx] = self.l2m(self.INDEX[msg_idx])
      return self.CACHE[msg_idx]
    except IndexError:
      return (None, None, None, None, b36(0),
              '(not in index)', '(not in index)', None, None)

  def get_conversation(self, msg_idx):
    return self.get_msg_by_idx(
             int(self.get_msg_by_idx(msg_idx)[self.MSG_CONV_ID], 36))

  def get_replies(self, msg_info=None, msg_idx=None):
    if not msg_info: msg_info = self.get_msg_by_idx(msg_idx)
    return [self.get_msg_by_idx(int(r, 36)) for r
            in msg_info[self.MSG_REPLIES].split(',') if r]

  def get_tags(self, msg_info=None, msg_idx=None):
    if not msg_info: msg_info = self.get_msg_by_idx(msg_idx)
    return [r for r in msg_info[self.MSG_TAGS].split(',') if r]

  def add_tag(self, session, tag_id, msg_info=None, msg_idxs=None):
    pls = PostingList(session, '%s:tag' % tag_id)
    if not msg_idxs:
      msg_idxs = [int(msg_info[self.MSG_IDX], 36)]
    session.ui.mark('Tagging %d messages (%s)' % (len(msg_idxs), tag_id))
    for msg_idx in list(msg_idxs):
      for reply in self.get_replies(msg_idx=msg_idx):
        msg_idxs.add(int(reply[self.MSG_IDX], 36))
        if msg_idx % 1000 == 0: self.CACHE = {}
    for msg_idx in msg_idxs:
      msg_info = self.get_msg_by_idx(msg_idx)
      tags = set([r for r in msg_info[self.MSG_TAGS].split(',') if r])
      tags.add(tag_id)
      msg_info[self.MSG_TAGS] = ','.join(list(tags))
      self.INDEX[msg_idx] = self.m2l(msg_info)
      pls.append(msg_info[self.MSG_IDX])
      if msg_idx % 1000 == 0: self.CACHE = {}
    pls.save()
    self.CACHE = {}

  def remove_tag(self, session, tag_id, msg_info=None, msg_idxs=None):
    pls = PostingList(session, '%s:tag' % tag_id)
    if not msg_idxs:
      msg_idxs = [int(msg_info[self.MSG_IDX], 36)]
    session.ui.mark('Untagging %d messages (%s)' % (len(msg_idxs), tag_id))
    for msg_idx in list(msg_idxs):
      for reply in self.get_replies(msg_idx=msg_idx):
        msg_idxs.add(int(reply[self.MSG_IDX], 36))
        if msg_idx % 1000 == 0: self.CACHE = {}
    for msg_idx in msg_idxs:
      msg_info = self.get_msg_by_idx(msg_idx)
      tags = set([r for r in msg_info[self.MSG_TAGS].split(',') if r])
      if tag_id in tags:
        tags.remove(tag_id)
        msg_info[self.MSG_TAGS] = ','.join(list(tags))
        self.INDEX[msg_idx] = self.m2l(msg_info)
      pls.remove(msg_info[self.MSG_IDX])
      if msg_idx % 1000 == 0: self.CACHE = {}
    pls.save()
    self.CACHE = {}

  def search(self, session, searchterms, keywords=None):
    if keywords:
      def hits(term):
        return set(keywords.get(term, []))
    else:
      def hits(term):
        session.ui.mark('Searching for %s' % term)
        return PostingList(session, term).hits()

    if len(self.CACHE.keys()) > 5000: self.CACHE = {}
    r = []
    for term in searchterms:
      if term in STOPLIST:
        if session:
          session.ui.warning('Ignoring common word: %s' % term)
        continue

      if term[0] in ('-', '+'):
        op = term[0]
        term = term[1:]
      else:
        op = None

      r.append((op, []))
      rt = r[-1][1]
      term = term.lower()

      if term.startswith('body:'):
        rt.extend(hits(term[5:]))
      elif ':' in term:
        t = term.split(':', 1)
        rt.extend(hits('%s:%s' % (t[1], t[0])))
      else:
        rt.extend(hits(term))

    if r:
      results = set(r[0][1])
      for (op, rt) in r[1:]:
        if op == '+':
          results |= set(rt)
        elif op == '-':
          results -= set(rt)
        else:
          results &= set(rt)
      # Sometimes the scan gets aborted...
      if not keywords:
        results -= set([b36(len(self.INDEX))])
    else:
      results = set()

    results = [int(r, 36) for r in results]
    if session:
      session.ui.mark('Found %d results' % len(results))
    return results

  def sort_results(self, session, results, how=None):
    force = how or False
    how = how or self.config.get('default_order', 'reverse_date')
    sign = how.startswith('rev') and -1 or 1
    sort_max = self.config.get('sort_max', 5000)
    if not results: return

    if len(results) > sort_max and not force:
      session.ui.warning(('Over sort_max (%s) results, sorting badly.'
                          ) % sort_max)
      results.sort()
      if sign < 0: results.reverse()
      leftovers = results[sort_max:]
      results[sort_max:] = []
    else:
      leftovers = []

    session.ui.mark('Sorting messages in %s order...' % how)
    try:
      if how == 'unsorted':
        pass
      elif how.endswith('index'):
        results.sort()
      elif how.endswith('random'):
        now = time.time()
        results.sort(key=lambda k: sha1b64('%s%s' % (now, k)))
      elif how.endswith('date'):
        results.sort(key=lambda k: long(self.get_msg_by_idx(k)[self.MSG_DATE], 36))
      elif how.endswith('from'):
        results.sort(key=lambda k: self.get_msg_by_idx(k)[self.MSG_FROM])
      elif how.endswith('subject'):
        results.sort(key=lambda k: self.get_msg_by_idx(k)[self.MSG_SUBJECT])
      else:
        session.ui.warning('Unknown sort order: %s' % how)
        results.extend(leftovers)
        return False
    except:
      session.ui.warning('Sort failed, sorting badly.  Partial index?')

    if sign < 0: results.reverse()

    if 'flat' not in how:
      conversations = [int(self.get_msg_by_idx(r)[self.MSG_CONV_ID], 36)
                       for r in results]
      results[:] = []
      chash = {}
      for c in conversations:
        if c not in chash:
          results.append(c)
          chash[c] = 1

    results.extend(leftovers)

    session.ui.mark('Sorted messages in %s order' % how)
    return True


class NullUI(object):

  def print_key(self, key, config): pass
  def reset_marks(self): pass
  def mark(self, progress): pass

  def say(self, text='', newline='\n', fd=sys.stdout):
    fd.write(text.encode('utf-8')+newline)
    fd.flush()

  def notify(self, message): self.say(str(message))
  def warning(self, message): self.say('Warning: %s' % message)
  def error(self, message): self.say('Error: %s' % message)

  def print_intro(self, help=False):
    self.say(ABOUT+'\nFor instructions type `help`, press <CTRL-D> to quit.\n')

  def print_help(self, commands, tags):
    self.say('Commands:')
    last_rank = None
    cmds = commands.keys()
    cmds.sort(key=lambda k: commands[k][3])
    for c in cmds:
      cmd, args, explanation, rank = commands[c]
      if not rank: continue

      if last_rank and int(rank/10) != last_rank: self.say()
      last_rank = int(rank/10)

      self.say('    %s|%-8.8s %-15.15s %s' % (c[0], cmd.replace('=', ''),
                                              args and ('<%s>' % args) or '',
                                              explanation))
    if tags:
      self.say('\nTags:  (use a tag as a command to display tagged messages)',
               '\n  ')
      tags.sort()
      for i in range(0, len(tags)):
        self.say('%-18.18s ' % tags[i], newline=(i%4==3) and '\n  ' or '')
    self.say('\n')


class TextUI(NullUI):
  def __init__(self):
    self.times = []

  def print_key(self, key, config):
    if ':' in key:
      key, subkey = key.split(':', 1)
    else:
      subkey = None

    if key in config:
      if key in config.INTS:
        self.say('%s = %s (int)' % (key, config.get(key)))
      else:
        val = config.get(key)
        if subkey:
          if subkey in val:
            self.say('%s:%s = %s' % (key, subkey, val[subkey]))
          else:
            self.say('%s:%s is unset' % (key, subkey))
        else:
          self.say('%s = %s' % (key, config.get(key)))
    else:
      self.say('%s is unset' % key)

  def reset_marks(self):
    t = self.times
    self.times = []
    if t:
      result = 'Elapsed: %.3fs (%s)' % (t[-1][0] - t[0][0], t[-1][1])
      self.say('%s%s' % (result, ' ' * (79-len(result))))
      return t[-1][0] - t[0][0]
    else:
      return 0

  def mark(self, progress):
    self.say('  %s%s\r' % (progress, ' ' * (77-len(progress))),
             newline='', fd=sys.stderr)
    self.times.append((time.time(), progress))

  def name(self, sender):
    words = re.sub('["<>]', '', sender).split()
    nomail = [w for w in words if not '@' in w]
    if nomail: return ' '.join(nomail)
    return ' '.join(words)

  def names(self, senders):
    if len(senders) > 3:
      return re.sub('["<>]', '', ','.join([x.split()[0] for x in senders]))
    return ','.join([self.name(s) for s in senders])

  def compact(self, namelist, maxlen):
    l = len(namelist)
    while l > maxlen:
      namelist = re.sub(',[^, \.]+,', ',,', namelist, 1)
      if l == len(namelist): break
      l = len(namelist)
    namelist = re.sub(',,,+,', ' .. ', namelist, 1)
    return namelist

  def display_results(self, idx, results, start=0, end=None, num=20):
    if not results: return

    if end: start = end - num
    if start > len(results): start = len(results)
    if start < 0: start = 0

    clen = max(3, len('%d' % len(results)))
    cfmt = '%%%d.%ds' % (clen, clen)

    count = 0
    for mid in results[start:start+num]:
      count += 1
      try:
        msg_info = idx.get_msg_by_idx(mid)
        msg_subj = msg_info[idx.MSG_SUBJECT]

        msg_from = [msg_info[idx.MSG_FROM]]
        msg_from.extend([r[idx.MSG_FROM] for r in idx.get_replies(msg_info)])

        msg_date = [msg_info[idx.MSG_DATE]]
        msg_date.extend([r[idx.MSG_DATE] for r in idx.get_replies(msg_info)])
        msg_date = datetime.date.fromtimestamp(max([
                                                int(d, 36) for d in msg_date]))

        msg_tags = '<'.join(sorted([re.sub("^.*/", "", idx.config['tag'].get(t, t))
                                    for t in idx.get_tags(msg_info=msg_info)]))
        msg_tags = msg_tags and (' <%s' % msg_tags) or '  '

        sfmt = '%%-%d.%ds%%s' % (41-(clen+len(msg_tags)),41-(clen+len(msg_tags)))
        self.say((cfmt+' %4.4d-%2.2d-%2.2d %-25.25s '+sfmt
                  ) % (start + count,
                       msg_date.year, msg_date.month, msg_date.day,
                       self.compact(self.names(msg_from), 25),
                       msg_subj, msg_tags))
      except:
        raise
        self.say('-- (not in index: %s)' % mid)
    session.ui.mark(('Listed %d-%d of %d results'
                     ) % (start+1, start+count, len(results)))
    return (start, count)



class UsageError(Exception):
  pass


class ConfigManager(dict):

  index = None

  INTS = ('postinglist_kb', 'sort_max', 'num_results', 'fd_cache_size')
  STRINGS = ('mailindex_file', 'postinglist_dir', 'default_order')
  DICTS = ('mailbox', 'tag', 'filter', 'filter_terms', 'filter_tags')

  def workdir(self):
    return os.environ.get('MAILPILE_HOME', os.path.expanduser('~/.mailpile'))

  def conffile(self):
    return os.path.join(self.workdir(), 'config.rc')

  def parse_unset(self, session, arg):
    key = arg.strip().lower()
    if key in self:
      del self[key]
    elif ':' in key and key.split(':', 1)[0] in self.DICTS:
      key, subkey = key.split(':', 1)
      if key in self and subkey in self[key]:
        del self[key][subkey]
    session.ui.print_key(key, self)
    return True

  def parse_set(self, session, line):
    key, val = [k.strip() for k in line.split('=', 1)]
    key = key.lower()
    if key in self.INTS:
      self[key] = int(val)
    elif key in self.STRINGS:
      self[key] = val
    elif ':' in key and key.split(':', 1)[0] in self.DICTS:
      key, subkey = key.split(':', 1)
      if key not in self:
        self[key] = {}
      self[key][subkey] = val
    else:
      raise UsageError('Unknown key in config: %s' % key)
    session.ui.print_key(key, self)
    return True

  def load(self, session):
    if not os.path.exists(self.workdir()):
      session.ui.notify('Creating: %s' % self.workdir())
      os.mkdir(self.workdir())
    else:
      self.index = None
      for key in (self.INTS + self.STRINGS):
        if key in self: del self[key]
      try:
        fd = open(self.conffile(), 'r')
        for line in fd:
          line = line.strip()
          if line.startswith('#') or not line:
            pass
          elif '=' in line:
            self.parse_set(session, line)
          else:
            raise UsageError('Bad line in config: %s' % line)
        fd.close()
      except IOError:
        pass

  def save(self):
    if not os.path.exists(self.workdir()):
      session.ui.notify('Creating: %s' % self.workdir())
      os.mkdir(self.workdir())
    fd = open(self.conffile(), 'w')
    fd.write('# Mailpile autogenerated configuration file\n')
    for key in sorted(self.keys()):
      if key in self.DICTS:
        for subkey in sorted(self[key].keys()):
          fd.write('%s:%s = %s\n' % (key, subkey, self[key][subkey]))
      else:
        fd.write('%s = %s\n' % (key, self[key]))
    fd.close()

  def nid(self, what):
    if what not in self or not self[what]:
      return '0'
    else:
      return b36(1+max([int(k, 36) for k in self[what]]))

  def open_mailbox(self, session, mailbox_id, mailbox_fn):
    pfn = os.path.join(self.workdir(), 'pickled-mailbox.%s' % mailbox_id)
    try:
      session.ui.mark(('%s: Updating: %s'
                       ) % (mailbox_id, mailbox_fn))
      mbox = cPickle.load(open(pfn, 'r'))
    except (IOError, EOFError):
      session.ui.mark(('%s: Opening: %s (may take a while)'
                       ) % (mailbox_id, mailbox_fn))
      mbox = IncrementalMbox(mailbox_fn)
      mbox.save(session, to=pfn)
    return mbox

  def get_filters(self):
    filters = self.get('filter', {}).keys()
    filters.sort(key=lambda k: int(k, 36))
    flist = []
    for fid in filters:
      comment = self.get('filter', {}).get(fid, '')
      terms = unicode(self.get('filter_terms', {}).get(fid, ''))
      tags = unicode(self.get('filter_tags', {}).get(fid, ''))
      flist.append((fid, terms, tags, comment))
    return flist

  def get_mailboxes(self):
    def fmt_mbxid(k):
      k = b36(int(k, 36))
      if len(k) > 3:
        raise ValueError('Mailbox ID too large: %s' % k)
      return ('000'+k)[-3:]
    mailboxes = self['mailbox'].keys()
    mailboxes.sort()
    mailboxes.reverse()
    return [(fmt_mbxid(k), self['mailbox'][k]) for k in mailboxes]

  def history_file(self):
    return self.get('history_file',
                    os.path.join(self.workdir(), 'history'))

  def mailindex_file(self):
    return self.get('mailindex_file',
                    os.path.join(self.workdir(), 'mailpile.idx'))

  def postinglist_dir(self):
    d = self.get('postinglist_dir',
                 os.path.join(self.workdir(), 'search'))
    if not os.path.exists(d): os.mkdir(d)
    return d

  def get_index(self, session):
    if self.index: return self.index
    idx = self.index = MailIndex(self)
    idx.load(session)
    return idx


class Session(object):

  ui = NullUI()
  order = None
  results = []
  searched = []
  displayed = (0, 0)
  interactive = False

  def __init__(self, config):
    self.config = config

  def error(self, message):
    self.ui.error(message)
    if not self.interactive: sys.exit(1)


COMMANDS = {
  'A:': ('add=',     'path/to/mbox',  'Add a mailbox',                      54),
  'F:': ('filter=',  'options',       'Add/edit/delete auto-tagging rules', 56),
  'h':  ('help',     '',              'Print help on how to use mailpile',   0),
  'L':  ('load',     '',              'Load the metadata index',            11),
  'n':  ('next',     '',              'Display next page of results',       31),
  'o:': ('order=',   '[rev-]what',   ('Sort by: date, from, subject, '
                                      'random or index'),                   33),
  'O':  ('optimize', '',              'Optimize the keyword search index',  12),
  'p':  ('previous', '',              'Display previous page of results',   32),
  'P:': ('print=',   'var',           'Print a setting',                    52),
  'R':  ('rescan',   '',              'Scan all mailboxes for new messages',13),
  's:': ('search=',  'terms ...',     'Search!',                            30),
  'S:': ('set=',     'var=value',     'Change a setting',                   50),
  't:': ('tag=',     '[+|-]tag msg',  'Tag or untag search results',        34),
  'T:': ('addtag=',  'tag',           'Create a new tag',                   55),
  'U:': ('unset=',   'var',           'Reset a setting to the default',     51),
}
def Action_Tag(session, opt, arg, save=True):
  idx = session.config.get_index(session)
  session.ui.reset_marks()
  try:
    words = arg.split()
    op = words[0][0]
    tag = words[0][1:]
    tag_id = [t for t in session.config['tag']
              if session.config['tag'][t].lower() == tag.lower()][0]

    msg_ids = set()
    for what in words[1:]:
      if what.lower() == 'these':
        b, c = session.displayed
        msg_ids |= set(session.results[b:b+c])
      elif what.lower() == 'all':
        msg_ids |= set(session.results)
      elif what.startswith('='):
        try:
          msg_ids.add(session.results[int(what[1:], 36)])
        except:
          session.ui.warning('What message is %s?' % (what, ))
      elif '-' in what:
        try:
          b, e = what.split('-')
          msg_ids |= set(session.results[int(b)-1:int(e)])
        except:
          session.ui.warning('What message is %s?' % (what, ))
      else:
        try:
          msg_ids.add(session.results[int(what)-1])
        except:
          session.ui.warning('What message is %s?' % (what, ))

    if op == '-':
      idx.remove_tag(session, tag_id, msg_idxs=msg_ids)
    else:
      idx.add_tag(session, tag_id, msg_idxs=msg_ids)

    if save:
      idx.save(session)
    session.ui.reset_marks()
    return True

  except (TypeError, ValueError, IndexError):
    session.ui.error('That made no sense: %s %s' % (opt, arg))
    return False

def Action_Filter_Add(session, config, flags, args):
  terms = ('new' in flags) and ['*'] or session.searched
  if args and args[0][0] == '=':
    tag_id = args.pop(0)[1:]
  else:
    tag_id = config.nid('filter')

  if not terms or (len(args) < 1):
    raise UsageError('Need search term and flags')

  tags, tids = [], []
  while args and args[0][0] in ('-', '+'):
    tag = args.pop(0)
    tags.append(tag)
    tids.append([tag[0]+t for t in config['tag']
                 if config['tag'][t].lower() == tag[1:].lower()][0])

  if not args:
    args = ['Filter for %s' % ' '.join(tags)]

  if 'notag' not in flags and 'new' not in flags:
    for tag in tags:
      if not Action_Tag(session, 'filter/tag', '%s all' % tag, save=False):
        raise UsageError()

  if (config.parse_set(session, ('filter:%s=%s'
                                 ) % (tag_id, ' '.join(args)))
  and config.parse_set(session, ('filter_tags:%s=%s'
                                 ) % (tag_id, ' '.join(tids)))
  and config.parse_set(session, ('filter_terms:%s=%s'
                                 ) % (tag_id, ' '.join(terms)))):
    config.get_index(session).save(session)
    config.save()
    session.ui.reset_marks()
  else:
    raise Exception('That failed, not sure why?!')

def Action_Filter_Delete(session, config, flags, args):
  if len(args) < 1 or args[0] not in config.get('filter', {}):
    raise UsageError('Delete what?')

  fid = args[0]
  if (config.parse_unset(session, 'filter:%s' % fid)
  and config.parse_unset(session, 'filter_tags:%s' % fid)
  and config.parse_unset(session, 'filter_terms:%s' % fid)):
    config.save()
  else:
    raise Exception('That failed, not sure why?!')

def Action_Filter_Move(session, config, flags, args):
  raise Exception('Unimplemented')

def Action_Filter_List(session, config, flags, args):
  session.ui.say('  ID  Tags                   Terms')
  for fid, terms, tags, comment in config.get_filters():
    session.ui.say((' %3.3s %-23.23s %-20.20s %s'
                    ) % (fid,
        ' '.join(['%s%s' % (t[0], config['tag'][t[1:]]) for t in tags.split()]),
                       (terms == '*') and '(all new mail)' or terms or '(none)',
                         comment or '(none)'))

def Action_Filter(session, opt, arg):
  config = session.config
  args = arg.split()
  flags = []
  while args and args[0] in ('add', 'set', 'delete', 'move', 'list',
                             'new', 'notag'):
    flags.append(args.pop(0))
  try:
    if 'delete' in flags:
      return Action_Filter_Delete(session, config, flags, args)
    elif 'move' in flags:
      return Action_Filter_Move(session, config, flags, args)
    elif 'list' in flags:
      return Action_Filter_List(session, config, flags, args)
    else:
      return Action_Filter_Add(session, config, flags, args)
  except UsageError:
    pass
  except Exception, e:
    session.error(e)
    return
  session.ui.say(
    'Usage: filter [new] [notag] [=ID] <[+|-]tags ...> [description]\n'
    '       filter delete <id>\n'
    '       filter move <id> <pos>\n'
    '       filter list')

def Action(session, opt, arg):
  config = session.config
  num_results = config.get('num_results', 20)

  if not opt or opt in ('h', 'help'):
    session.ui.print_help(COMMANDS, session.config['tag'].values())

  elif opt in ('A', 'add'):
    if os.path.exists(arg):
      arg = os.path.abspath(arg)
      if config.parse_set(session,
                          'mailbox:%s=%s' % (config.nid('mailbox'), arg)):
        config.save()
    else:
      session.error('No such file/directory: %s' % arg)

  elif opt in ('T', 'addtag'):
    if (arg
    and ' ' not in arg
    and arg.lower() not in [v.lower() for v in config['tag'].values()]):
      if config.parse_set(session,
                          'tag:%s=%s' % (config.nid('tag'), arg)):
        config.save()
    else:
      session.error('Invalid tag: %s' % arg)

  elif opt in ('F', 'filter'):
    Action_Filter(session, opt, arg)

  elif opt in ('O', 'optimize'):
    try:
      idx = config.get_index(session)
      filecount = PostingList.Optimize(session, idx,
                                       force=(arg == 'harder'))
      session.ui.reset_marks()
    except KeyboardInterrupt:
      session.ui.mark('Aborted')
      session.ui.reset_marks()

  elif opt in ('P', 'print'):
    session.ui.print_key(arg.strip().lower(), config)

  elif opt in ('U', 'unset'):
    if config.parse_unset(session, arg): config.save()

  elif opt in ('S', 'set'):
    if config.parse_set(session, arg): config.save()

  elif opt in ('R', 'rescan'):
    idx = config.get_index(session)
    session.ui.reset_marks()
    count = 0
    try:
      for fid, fpath in config.get_mailboxes():
        count += idx.scan_mailbox(session, fid, fpath, config.open_mailbox)
        session.ui.mark('\n')
    except KeyboardInterrupt:
      session.ui.mark('Aborted')
      count = 1
    if count:
      idx.save(session)
    else:
      session.ui.mark('Nothing changed')
    session.ui.reset_marks()

  elif opt in ('L', 'load'):
    config.index = None
    session.results = []
    session.searched = []
    config.get_index(session)
    session.ui.reset_marks()

  elif opt in ('n', 'next'):
    idx = config.get_index(session)
    session.ui.reset_marks()
    pos, count = session.displayed
    session.displayed = session.ui.display_results(idx, session.results,
                                                   start=pos+count,
                                                   num=num_results)
    session.ui.reset_marks()

  elif opt in ('p', 'previous'):
    idx = config.get_index(session)
    session.ui.reset_marks()
    pos, count = session.displayed
    session.displayed = session.ui.display_results(idx, session.results,
                                                   end=pos,
                                                   num=num_results)
    session.ui.reset_marks()

  elif opt in ('t', 'tag'):
    Action_Tag(session, opt, arg)

  elif opt in ('o', 'order'):
    idx = config.get_index(session)
    session.ui.reset_marks()
    session.order = arg or None
    idx.sort_results(session, session.results,
                     how=session.order)
    session.displayed = session.ui.display_results(idx, session.results,
                                                   num=num_results)
    session.ui.reset_marks()

  elif (opt in ('s', 'search')
        or opt.lower() in [t.lower() for t in config['tag'].values()]):
    idx = config.get_index(session)
    session.ui.reset_marks()

    # FIXME: This is all rather dumb.  Make it smarter!
    if opt not in ('s', 'search'):
      tid = [t for t in config['tag'] if config['tag'][t].lower() == opt.lower()]
      session.searched = ['tag:%s' % tid[0]]
    elif ':' in arg or '-' in arg or '+' in arg:
      session.searched = arg.lower().split()
    else:
      session.searched = re.findall(WORD_REGEXP, arg.lower())

    session.results = list(idx.search(session, session.searched))
    idx.sort_results(session, session.results,
                     how=session.order)
    session.displayed = session.ui.display_results(idx, session.results,
                                                   num=num_results)
    session.ui.reset_marks()

  else:
    raise UsageError('Unknown command: %s' % opt)



def Interact(session):
  import readline
  try:
    readline.read_history_file(session.config.history_file())
  except IOError:
    pass
  readline.set_history_length(100)

  try:
    while True:
      opt = raw_input('mailpile> ').decode('utf-8').strip()
      if opt:
        if ' ' in opt:
          opt, arg = opt.split(' ', 1)
        else:
          arg = ''
        try:
          Action(session, opt, arg)
        except UsageError, e:
          session.error(e)
  except EOFError:
    print

  readline.write_history_file(session.config.history_file())


if __name__ == "__main__":
  re.UNICODE = 1
  re.LOCALE = 1

  session = Session(ConfigManager())
  session.config.load(session)
  session.ui = TextUI()

  # Set globals from config here ...
  APPEND_FD_CACHE_SIZE = session.config.get('fd_cache_size',
                                            APPEND_FD_CACHE_SIZE)

  try:
    opts, args = getopt.getopt(sys.argv[1:],
                               ''.join(COMMANDS.keys()),
                               [v[0] for v in COMMANDS.values()])
    for opt, arg in opts:
      Action(session, opt.replace('-', ''), arg)
    if args:
      Action(session, args[0], ' '.join(args[1:]))

  except (getopt.GetoptError, UsageError), e:
    session.error(e)

  if not opts and not args:
    session.interactive = session.ui.interactive = True
    session.ui.print_intro(help=True)
    Interact(session)

