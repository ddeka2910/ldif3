"""ldif - generate and parse LDIF data (see RFC 2849).

See http://www.python-ldap.org/ for details.

$Id: ldif.py,v 1.74 2014/03/12 23:11:26 stroeder Exp $

Python compability note:
Tested with Python 2.0+, but should work with Python 1.5.2+.
"""

__version__ = '2.4.15'

__all__ = [
    # constants
    'ldif_pattern',
    # classes
    'LDIFWriter',
    'LDIFParser',
    'LDIFRecordList',
    'LDIFCopy',
]

import urlparse
import urllib
import base64
import re
import types

attrtype_pattern = r'[\w;.-]+(;[\w_-]+)*'
attrvalue_pattern = r'(([^,]|\\,)+|".*?")'
attrtypeandvalue_pattern = attrtype_pattern + r'[ ]*=[ ]*' + attrvalue_pattern
rdn_pattern = attrtypeandvalue_pattern + r'([ ]*\+[ ]*' + \
    attrtypeandvalue_pattern + r')*[ ]*'
dn_pattern = rdn_pattern + r'([ ]*,[ ]*' + rdn_pattern + r')*[ ]*'
dn_regex = re.compile('^%s$' % dn_pattern)

ldif_pattern = ('^((dn(:|::) %(dn_pattern)s)|(%(attrtype_pattern)'
    's(:|::) .*)$)+' % vars())

MOD_OP_INTEGER = {
    'add': 0,
    'delete': 1,
    'replace': 2,
}

MOD_OP_STR = {
    0: 'add',
    1: 'delete',
    2: 'replace',
}

CHANGE_TYPES = ['add', 'delete', 'modify', 'modrdn']
valid_changetype_dict = {}
for c in CHANGE_TYPES:
    valid_changetype_dict[c] = None


def is_dn(s):
    """Return True if s is a LDAP DN."""
    if s == '':
        return True
    rm = dn_regex.match(s)
    return rm is not None and rm.group(0) == s


SAFE_STRING_PATTERN = '(^(\000|\n|\r| |:|<)|[\000\n\r\200-\377]+|[ ]+$)'
safe_string_re = re.compile(SAFE_STRING_PATTERN)


def list_dict(l):
    """Return a dict with the lowercased items of l as keys."""
    return dict([(i.lower(), None) for i in (l or [])])


class LDIFWriter:
    """Write LDIF entry or change records to file object.

    Copy LDIF input to a file output object containing all data retrieved
    via URLs.
    """

    def __init__(self, output_file, base64_attrs=None, cols=76, line_sep='\n'):
        """
        output_file
            file object for output
        base64_attrs
            list of attribute types to be base64-encoded in any case
        cols
            Specifies how many columns a line may have before it's
            folded into many lines.
        line_sep
            String used as line separator
        """
        self._output_file = output_file
        self._base64_attrs = list_dict(base64_attrs)
        self._cols = cols
        self._line_sep = line_sep
        self.records_written = 0

    def _fold_line(self, line):
        """Write string line as one or more folded lines."""
        if len(line) <= self._cols:
            self._output_file.write(line)
            self._output_file.write(self._line_sep)
        else:
            pos = self._cols
            self._output_file.write(line[0:self._cols])
            self._output_file.write(self._line_sep)
            while pos < len(line):
                self._output_file.write(' ')
                end = min(len(line), pos + self._cols - 1)
                self._output_file.write(line[pos:end])
                self._output_file.write(self._line_sep)
                pos = end

    def _needs_base64_encoding(self, attr_type, attr_value):
        """Return True if attr_value has to be base-64 encoded.

        This is the case because of special chars or because attr_type is in
        self._base64_attrs
        """
        return attr_type.lower() in self._base64_attrs or \
                safe_string_re.search(attr_value) is not None

    def _unparse_attr(self, attr_type, attr_value):
        """Write a single attribute type/value pair."""
        if self._needs_base64_encoding(attr_type, attr_value):
            encoded = base64.encodestring(attr_value).replace('\n', '')
            self._fold_line(':: '.join([attr_type, encoded]))
        else:
            self._fold_line(': '.join([attr_type, attr_value]))

    def _unparse_entry_record(self, entry):
        """
        entry
            dictionary holding an entry
        """
        for attr_type in sorted(entry.keys()):
            for attr_value in entry[attr_type]:
                self._unparse_attr(attr_type, attr_value)

    def _unparse_change_record(self, modlist):
        """
        modlist
            list of additions (2-tuple) or modifications (3-tuple)
        """
        mod_len = len(modlist[0])
        if mod_len == 2:
            changetype = 'add'
        elif mod_len == 3:
            changetype = 'modify'
        else:
            raise ValueError("modlist item of wrong length")
        self._unparse_attr('changetype', changetype)
        for mod in modlist:
            if mod_len == 2:
                mod_type, mod_vals = mod
            elif mod_len == 3:
                mod_op, mod_type, mod_vals = mod
                self._unparse_attr(MOD_OP_STR[mod_op], mod_type)
            else:
                raise ValueError("Subsequent modlist item of wrong length")
            if mod_vals:
                for mod_val in mod_vals:
                    self._unparse_attr(mod_type, mod_val)
            if mod_len == 3:
                self._output_file.write('-' + self._line_sep)

    def unparse(self, dn, record):
        """
        dn
            string-representation of distinguished name
        record
            Either a dictionary holding the LDAP entry {attrtype:record}
            or a list with a modify list like for LDAPObject.modify().
        """
        self._unparse_attr('dn', dn)
        if isinstance(record, types.DictType):
            self._unparse_entry_record(record)
        elif isinstance(record, types.ListType):
            self._unparse_change_record(record)
        else:
            raise ValueError("Argument record must be dictionary or list")
        self._output_file.write(self._line_sep)
        self.records_written += 1


class LDIFParser:
    """Base class for a LDIF parser.

    Applications should sub-class this class and override method handle() to
    implement something meaningful.

    Public class attributes:

    records_read
        Counter for records processed so far
    """

    def _strip_line_sep(self, s):
        """Strip trailing line separators from s, but no other whitespaces."""
        if s[-2:] == '\r\n':
            return s[:-2]
        elif s[-1:] == '\n':
            return s[:-1]
        else:
            return s

    def __init__(
        self,
        input_file,
        ignored_attr_types=None,
        max_entries=0,
        process_url_schemes=None,
        line_sep='\n'
    ):
        """
        Parameters:
        input_file
            File-object to read the LDIF input from
        ignored_attr_types
            Attributes with these attribute type names will be ignored.
        max_entries
            If non-zero specifies the maximum number of entries to be
            read from f.
        process_url_schemes
            List containing strings with URLs schemes to process with urllib.
            An empty list turns off all URL processing and the attribute
            is ignored completely.
        line_sep
            String used as line separator
        """
        self._input_file = input_file
        self._max_entries = max_entries
        self._process_url_schemes = list_dict(process_url_schemes)
        self._ignored_attr_types = list_dict(ignored_attr_types)
        self._line_sep = line_sep
        self.records_read = 0

    def handle(self, dn, entry):
        """Proces a single content LDIF record.

        This method should be implemented by applications using LDIFParser.
        """

    def _unfold_line(self):
        """Unfold several folded lines with trailing space into one line."""
        unfolded_lines = [self._strip_line_sep(self._line)]
        self._line = self._input_file.readline()
        while self._line and self._line[0] == ' ':
            unfolded_lines.append(self._strip_line_sep(self._line[1:]))
            self._line = self._input_file.readline()
        return ''.join(unfolded_lines)

    def _parse_attr(self):
        """Parse a single attribute type/value pair from one or more lines."""
        unfolded_line = self._unfold_line()
        while unfolded_line and unfolded_line[0] == '#':
            unfolded_line = self._unfold_line()
        if not unfolded_line or unfolded_line in ['\n', '\r\n']:
            return None, None
        try:
            colon_pos = unfolded_line.index(':')
        except ValueError:
            return None, None
        attr_type = unfolded_line[0:colon_pos]
        value_spec = unfolded_line[colon_pos:colon_pos + 2]
        if value_spec == '::':
            attr_value = base64.decodestring(unfolded_line[colon_pos + 2:])
        elif value_spec == ':<':
            url = unfolded_line[colon_pos + 2:].strip()
            attr_value = None
            if self._process_url_schemes:
                u = urlparse.urlparse(url)
                if u[0] in self._process_url_schemes:
                    attr_value = urllib.urlopen(url).read()
        elif value_spec == ':\r\n' or value_spec == '\n':
            attr_value = ''
        else:
            attr_value = unfolded_line[colon_pos + 2:].lstrip()
        return attr_type, attr_value

    def parse(self):
        """Continously read and parse LDIF records."""
        self._line = self._input_file.readline()

        while self._line and (not self._max_entries or
                self.records_read < self._max_entries):

            # Reset record
            dn = None
            changetype = None
            entry = {}

            attr_type, attr_value = self._parse_attr()

            while attr_type is not None and attr_value is not None:
                if attr_type == 'dn':
                    if dn is not None:
                        raise ValueError('Two lines starting with dn: '
                            'in one record.')
                    if not is_dn(attr_value):
                        raise ValueError('No valid string-representation of '
                            'distinguished name %s.' % (repr(attr_value)))
                    dn = attr_value
                elif attr_type == 'version' and dn is None:
                    pass  # version = 1
                elif attr_type == 'changetype':
                    if dn is None:
                        raise ValueError('Read changetype: before getting '
                            'valid dn: line.')
                    if changetype is not None:
                        raise ValueError('Two lines starting with changetype: '
                            'in one record.')
                    if attr_value not in valid_changetype_dict:
                        raise ValueError('changetype value %s is invalid.'
                            % (repr(attr_value)))
                    changetype = attr_value
                elif attr_value is not None and \
                         attr_type.lower() not in self._ignored_attr_types:
                    if attr_type in entry:
                        entry[attr_type].append(attr_value)
                    else:
                        entry[attr_type] = [attr_value]

                attr_type, attr_value = self._parse_attr()

            if entry:
                self.handle(dn, entry)
                self.records_read += 1


class LDIFRecordList(LDIFParser):
    """Collect all records of LDIF input into a single list of 2-tuples.

    It can be a memory hog!
    """

    def __init__(
        self,
        input_file,
        ignored_attr_types=None,
        max_entries=0,
        process_url_schemes=None
    ):
        """See LDIFParser.__init__().

        Additional Parameters:
        all_records
            List instance for storing parsed records
        """
        LDIFParser.__init__(
            self,
            input_file,
            ignored_attr_types=ignored_attr_types,
            max_entries=max_entries,
            process_url_schemes=process_url_schemes)
        self.all_records = []

    def handle(self, dn, entry):
        """Append single record to dictionary of all records."""
        self.all_records.append((dn, entry))


class LDIFCopy(LDIFParser):
    """Copy LDIF input to LDIF output containing data retrieved via URLs."""

    def __init__(
        self,
        input_file,
        output_file,
        ignored_attr_types=None,
        max_entries=0,
        process_url_schemes=None,
        base64_attrs=None,
        cols=76,
        line_sep='\n'
    ):
        """See LDIFParser.__init__() and LDIFWriter.__init__()."""
        LDIFParser.__init__(
            self,
            input_file,
            ignored_attr_types=ignored_attr_types,
            max_entries=max_entries,
            process_url_schemes=process_url_schemes)
        self._output_ldif = LDIFWriter(
            output_file,
            base64_attrs=base64_attrs,
            cols=cols,
            line_sep=line_sep)

    def handle(self, dn, entry):
        """Write single LDIF record to output file."""
        self._output_ldif.unparse(dn, entry)
