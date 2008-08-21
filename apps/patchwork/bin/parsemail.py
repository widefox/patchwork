#!/usr/bin/python
#
# Patchwork - automated patch tracking system
# Copyright (C) 2008 Jeremy Kerr <jk@ozlabs.org>
#
# This file is part of the Patchwork package.
#
# Patchwork is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# Patchwork is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Patchwork; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA

import sys
import re
import datetime
import time
import operator
from email import message_from_file
from email.header import Header
from email.utils import parsedate_tz, mktime_tz

from patchparser import parse_patch
from patchwork.models import Patch, Project, Person, Comment

list_id_headers = ['List-ID', 'X-Mailing-List']

def find_project(mail):
    project = None
    listid_re = re.compile('.*<([^>]+)>.*', re.S)

    for header in list_id_headers:
        if header in mail:
            match = listid_re.match(mail.get(header))
            if not match:
                continue

            listid = match.group(1)

            try:
                project = Project.objects.get(listid = listid)
                break
            except:
                pass

    return project

def find_author(mail):

    from_header = mail.get('From').strip()
    (name, email) = (None, None)

    # tuple of (regex, fn)
    #  - where fn returns a (name, email) tuple from the match groups resulting
    #    from re.match().groups()
    from_res = [
        # for "Firstname Lastname" <example@example.com> style addresses
       (re.compile('"?(.*?)"?\s*<([^>]+)>'), (lambda g: (g[0], g[1]))),

       # for example@example.com (Firstname Lastname) style addresses
       (re.compile('"?(.*?)"?\s*\(([^\)]+)\)'), (lambda g: (g[1], g[0]))),

       # everything else
       (re.compile('(.*)'), (lambda g: (None, g[0]))),
    ]

    for regex, fn in from_res:
        match = regex.match(from_header)
        if match:
            (name, email) = fn(match.groups())
            break

    if email is None:
        raise Exception("Could not parse From: header")

    email = email.strip()
    if name is not None:
        name = name.strip()

    try:
        person = Person.objects.get(email = email)
    except Person.DoesNotExist:
        person = Person(name = name, email = email)

    return person

def mail_date(mail):
    t = parsedate_tz(mail.get('Date', ''))
    if not t:
        print "using now()"
        return datetime.datetime.utcnow()
    return datetime.datetime.utcfromtimestamp(mktime_tz(t))

def mail_headers(mail):
    return reduce(operator.__concat__,
            ['%s: %s\n' % (k, Header(v, header_name = k, \
                    continuation_ws = '\t').encode()) \
                for (k, v) in mail.items()])

def find_content(project, mail):
    patchbuf = None
    commentbuf = ''

    for part in mail.walk():
        if part.get_content_maintype() != 'text':
            continue

        #print "\t%s, %s" % \
        #    (part.get_content_subtype(), part.get_content_charset())

        charset = part.get_content_charset()
        if not charset:
            charset = mail.get_charset()
        if not charset:
            charset = 'utf-8'

        payload = unicode(part.get_payload(decode=True), charset, "replace")

        if part.get_content_subtype() == 'x-patch':
            patchbuf = payload

        if part.get_content_subtype() == 'plain':
            if not patchbuf:
                (patchbuf, c) = parse_patch(payload)
            else:
                c = payload

            if c is not None:
                commentbuf += c.strip() + '\n'

    patch = None
    comment = None

    if patchbuf:
        mail_headers(mail)
        patch = Patch(name = clean_subject(mail.get('Subject')),
                content = patchbuf, date = mail_date(mail),
                headers = mail_headers(mail))

    if commentbuf:
        if patch:
	    cpatch = patch
	else:
            cpatch = find_patch_for_comment(mail)
            if not cpatch:
                return (None, None)
        comment = Comment(patch = cpatch, date = mail_date(mail),
                content = clean_content(commentbuf),
                headers = mail_headers(mail))

    return (patch, comment)

def find_patch_for_comment(mail):
    # construct a list of possible reply message ids
    refs = []
    if 'In-Reply-To' in mail:
        refs.append(mail.get('In-Reply-To'))

    if 'References' in mail:
        rs = mail.get('References').split()
        rs.reverse()
        for r in rs:
            if r not in refs:
                refs.append(r)

    for ref in refs:
        patch = None

        # first, check for a direct reply
        try:
            patch = Patch.objects.get(msgid = ref)
            return patch
        except Patch.DoesNotExist:
            pass

        # see if we have comments that refer to a patch
        try:
            comment = Comment.objects.get(msgid = ref)
            return comment.patch
        except Comment.DoesNotExist:
            pass


    return None

re_re = re.compile('^(re|fwd?)[:\s]\s*', re.I)
prefix_re = re.compile('^\[.*\]\s*')
whitespace_re = re.compile('\s+')

def clean_subject(subject):
    subject = re_re.sub(' ', subject)
    subject = prefix_re.sub('', subject)
    subject = whitespace_re.sub(' ', subject)
    return subject.strip()

sig_re = re.compile('^(-{2,3} ?|_+)\n.*', re.S | re.M)
def clean_content(str):
    str = sig_re.sub('', str)
    return str.strip()

def main(args):
    mail = message_from_file(sys.stdin)

    # some basic sanity checks
    if 'From' not in mail:
        return 0

    if 'Subject' not in mail:
        return 0

    if 'Message-Id' not in mail:
        return 0

    hint = mail.get('X-Patchwork-Hint', '').lower()
    if hint == 'ignore':
        return 0;

    project = find_project(mail)
    if project is None:
        print "no project found"
        return 0

    msgid = mail.get('Message-Id').strip()

    author = find_author(mail)

    (patch, comment) = find_content(project, mail)

    if patch:
        author.save()
        patch.submitter = author
        patch.msgid = msgid
        patch.project = project
        try:
            patch.save()
        except Exception, ex:
            print ex.message

    if comment:
        author.save()
        # looks like the original constructor for Comment takes the pk
        # when the Comment is created. reset it here.
        if patch:
            comment.patch = patch
        comment.submitter = author
        comment.msgid = msgid
        try:
            comment.save()
        except Exception, ex:
            print ex.message

    return 0

if __name__ == '__main__':
    sys.exit(main(sys.argv))