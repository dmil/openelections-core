import re
import csv
import unicodecsv
import xlrd

from bs4 import BeautifulSoup

from openelex.base.load import BaseLoader
from openelex.models import RawResult
from openelex.lib.text import ocd_type_id, slugify
from .datasource import Datasource

class LoadResults(object):
    """Entry point for data loading.

    Determines appropriate loader for file and triggers load process.

    """

    def run(self, mapping):
        election_id = mapping['election']
        if 'precincts' in election_id:
            loader = OHPrecinctLoader()
        elif '2000' in election_id and 'primary' in election_id:
            loader = OHLoader2000Primary()
        elif '2008' in election_id and 'special' in election_id:
            loader = OHLoader2008Special()
        else:
            loader = OHHTMLoader()
        loader.run(mapping)


class OHBaseLoader(BaseLoader):
    datasource = Datasource()

    target_offices = set([
        'President - Vice Pres',
        'President and Vice President of the United States',
        'U.S. Senate',
        'U.S. Representative',
        'Representative in Congress',
        'Governor/Lieutenant Governor',
        'Attorney General',
        'Auditor of State',
        'Secretary of State',
        'Treasurer of State',
        'State Senate',
        'State Representative',
    ])

    district_offices = set([
        'U.S. Congress',
        'Representative in Congress',
        'State Senator',
        "House of Delegates",
    ])

    def _skip_row(self, row):
        """
        Should this row be skipped?

        This should be implemented in subclasses.
        """
        return False


class OH2012PrecinctLoader(OHBaseLoader):
    """
    Parse Ohio election results for 2012 precinct-level results files.
    """
    def load(self):
        with self._file_handle as xlsfile:
            results = []
            workbook = xlrd.open_workbook(xlsfile)
            worksheet = workbook.sheet_by_name('AllCounties')
            headers = worksheet.row(1)

            for row in reader:
                # Skip non-target offices
                if self._skip_row(row): 
                    continue
                elif 'state_legislative' in self.source:
                    results.extend(self._prep_state_leg_results(row))
                elif 'precinct' in self.source:
                    results.append(self._prep_precinct_result(row))
                else:
                    results.append(self._prep_county_result(row))
            RawResult.objects.insert(results)

           

class OH2010PrecinctLoader(OHBaseLoader):
    """
    Parse Ohio election results for 2010 precinct-level results (general
    and primary) contained in xlsx/xls files.
    """
    def load(self):
        with self._file_handle as xlsfile:
            results = []
            workbook = xlrd.open_workbook(xlsfile)
            worksheet = workbook.sheet_by_name('AllCounties')
            headers = worksheet.row(1)

            for row in reader:
                # Skip non-target offices
                if self._skip_row(row): 
                    continue
                elif 'state_legislative' in self.source:
                    results.extend(self._prep_state_leg_results(row))
                elif 'precinct' in self.source:
                    results.append(self._prep_precinct_result(row))
                else:
                    results.append(self._prep_county_result(row))
            RawResult.objects.insert(results)

    def _skip_row(self, row):
        return row['Office Name'].strip() not in self.target_offices

    def _build_contest_kwargs(self, row, primary_type):
        kwargs = {
            'office': row['Office Name'].strip(),
            'district': row['Office District'].strip(),
        }
        # Add party if it's a primary
        #TODO: QUESTION: Should semi-closed also have party?
        if primary_type == 'closed':
            kwargs['primary_party'] = row['Party'].strip()
        return kwargs

    def _build_candidate_kwargs(self, row):
        try:
            full_name = row['Candidate Name'].strip()
        except KeyError:
            # 2000 results use "Candidate" for the column name
            full_name = row['Candidate'].strip()
        slug = slugify(full_name, substitute='-')
        kwargs = {
            'full_name': full_name,
            #TODO: QUESTION: Do we need this? if so, needs a matching model field on RawResult
            'name_slug': slug,
        }
        return kwargs

    def _base_kwargs(self, row):
        "Build base set of kwargs for RawResult"
        # TODO: Can this just be called once?
        kwargs = self._build_common_election_kwargs()
        contest_kwargs = self._build_contest_kwargs(row, kwargs['primary_type'])
        candidate_kwargs = self._build_candidate_kwargs(row)
        kwargs.update(contest_kwargs)
        kwargs.update(candidate_kwargs)
        return kwargs

    def _get_state_ocd_id(self):
        # It looks like the OCD ID in this years mappings is
        # ocd-division/country:us/state:oh/precinct:all
        # We need to get rid of the "precinct:all" part to
        # build valid OCD IDs for the individual jurisdictions.
        return '/'.join(self.mapping['ocd_id'].split('/')[:-1])

    def _prep_state_leg_results(self, row):
        kwargs = self._base_kwargs(row)
        kwargs.update({
            'reporting_level': 'state_legislative',
            'winner': row['Winner'].strip(),
            'write_in': self._writein(row),
            'party': row['Party'].strip(),
        })
        try:
            kwargs['write_in'] = row['Write-In?'].strip() # at the contest-level
        except KeyError as e:
            pass
        results = []
        for field, val in row.items():
            clean_field = field.strip()
            # Legislative fields prefixed with LEGS
            if not clean_field.startswith('LEGS'):
                continue

            kwargs.update({
                'jurisdiction': clean_field,
                'ocd_id': "{}/sldl:{}".format(self._get_state_ocd_id(),
                    ocd_type_id(clean_field)),
                'votes': self._votes(val),
            })
            results.append(RawResult(**kwargs))
        return results

    def _prep_county_result(self, row):
        kwargs = self._base_kwargs(row)
        vote_brkdown_fields = [
           ('election_night_total', 'Election Night Votes'),
           ('absentee_total', 'Absentees Votes'),
           ('provisional_total', 'Provisional Votes'),
           ('second_absentee_total', '2nd Absentees Votes'),
        ]
        vote_breakdowns = {}
        for field, key in vote_brkdown_fields:
            try:
                vote_breakdowns[field] = row[key].strip()
            except KeyError:
                pass
        kwargs.update({
            'reporting_level': 'county',
            'jurisdiction': self.mapping['name'],
            'ocd_id': "{}/county:{}".format(self._get_state_ocd_id(),
                ocd_type_id(self.mapping['name'])),
            'party': row['Party'].strip(),
            'votes': self._votes(row['Total Votes']),
        })
        if (kwargs['office'] not in self.district_offices
                and kwargs['district'] != ''):
            kwargs['reporting_level'] = 'congressional_district_by_county'
            kwargs['reporting_district'] = kwargs['district']
            del kwargs['district']

        return RawResult(**kwargs)

    def _prep_precinct_result(self, row):
        kwargs = self._base_kwargs(row)
        vote_breakdowns = {
            'election_night_total': self._votes(row['Election Night Votes'])
        }
        precinct = "%s-%s" % (row['Election District'], row['Election Precinct'].strip())
        kwargs.update({
            'reporting_level': 'precinct',
            'jurisdiction': precinct,
            'ocd_id': "{}/precinct:{}".format(self._get_state_ocd_id(),
                ocd_type_id(precinct)),
            'party': row['Party'].strip(),
            'votes': self._votes(row['Election Night Votes']),
            'winner': row['Winner'],
            'write_in': self._writein(row),
            'vote_breakdowns': vote_breakdowns,
        })
        return RawResult(**kwargs)

    def _votes(self, val):
        """
        Returns cleaned version of votes or 0 if it's a non-numeric value.
        """
        if val.strip() == '':
            return 0

        try:
            return int(float(val))
        except ValueError:
            # Count'y convert value from string   
            return 0

    def _writein(self, row):
        # sometimes write-in field not present
        try:
            write_in = row['Write-In?'].strip()
        except KeyError:
            write_in = None
        return write_in


class MDLoader2002(MDBaseLoader):
    """
    Loads Maryland results for 2002.

    Format:

    Maryland results for 2002 are in a delimited text file where the delimiter
    is '|'.

    Fields:

     0: Office
     1: Office District - '-' is used to denote null values 
     2: County
     3: Last Name - "zz998" is used for write-in candidates
     4: Middle Name - "\N" is used to denote null values
     5: First Name - "Other Write-Ins" is used for write-in candidates
     6: Party
     7: Winner - Value is 0 or 1
     8: UNKNOWN - Values are "(Vote for One)", "(Vote for No More Than Three)", etc.
     9: Votes 
    10: UNKNOWN - Values are "\N" for every row
    
    Sample row:

    House of Delegates                                                  |32 |Anne Arundel County                               |Burton                                                      |W.              |Robert                                                      |Republican                                        |              0|(Vote for No More Than Three)                     |           1494|\N

    Notes:

    In the general election file, there are rows for judges and for
    "Statewide Ballot Questions".  The columns in these rows are shifted over,
    but we can ignore these rows since we're not interested in these offices.

    """

    def load(self):
        headers = [
            'office',
            'district',
            'jurisdiction',
            'family_name',
            'additional_name',
            'given_name',
            'party',
            'winner',
            'vote_type',
            'votes',
            'fill2'
        ]
        self._common_kwargs = self._build_common_election_kwargs()
        self._common_kwargs['reporting_level'] = 'county'
        # Store result instances for bulk loading
        results = []

        with self._file_handle as csvfile:
            reader = unicodecsv.DictReader(csvfile, fieldnames = headers, delimiter='|', encoding='latin-1')
            for row in reader:
                if self._skip_row(row):
                    continue
                
                rr_kwargs = self._common_kwargs.copy()
                if rr_kwargs['primary_type'] == 'closed':
                    rr_kwargs['primary_party'] = row['party'].strip()
                rr_kwargs.update(self._build_contest_kwargs(row))
                rr_kwargs.update(self._build_candidate_kwargs(row))
                rr_kwargs.update({
                    'party': row['party'].strip(),
                    'jurisdiction': row['jurisdiction'].strip(),
                    'office': row['office'].strip(),
                    'district': row['district'].strip(),
                    'votes': int(row['votes'].strip()),
                })
                results.append(RawResult(**rr_kwargs))
        RawResult.objects.insert(results)

    def _skip_row(self, row):
        return row['office'].strip() not in self.target_offices

    def _build_contest_kwargs(self, row):
        return {
            'office': row['office'].strip(),
            'district': row['district'].strip(),
        }

    def _build_candidate_kwargs(self, row):
        return {
            'family_name': row['family_name'].strip(),
            'given_name': row['given_name'].strip(),
            'additional_name': row['additional_name'].strip(),
        }


class MDLoader2000Primary(MDBaseLoader):
    office_choices = [
        "President and Vice President of the United States",
        "U.S. Senator",
        "Representative in Congress",
        "Judge of the Circuit Court",
        "Female Delegates and Alternate to the Democratic National Convention",
        "Female Delegates to the Democratic National Convention",
        "Male Delegates to the Democratic National Convention",
        "Male Delegates and Alternate to the Democratic National Convention",
        "Delegates to the Republican National Convention",
    ]

    def load(self):
        candidates = {}
        results = []
        last_office = None
        last_party = None
        last_district = None
        common_kwargs = self._build_common_election_kwargs()

        with self._file_handle as csvfile:
            reader = csv.reader(csvfile)
            for row in reader:
                if not len(row): 
                    continue # Skip blank lines

                # determine if this is a row with an office
                office, party, district = self._parse_header(row)
                if office:
                    # It's a header row
                    if office in self.target_offices:
                        # It's an office we care about. Save the office and
                        # party for the next row
                        last_office = office
                        last_party = party
                        last_district = district
                    else:
                        last_office = None
                        last_party = None
                        last_district = None
                elif last_office and row[0] == '':
                    # Candidate name row
                    candidates, winner_name = self._parse_candidates(row)
                elif last_office: # has to be a county result
                    new_results = self._parse_results(row, last_office,
                        last_party, last_district,
                        candidates, winner_name, common_kwargs)
                    results.extend(new_results)
        
        RawResult.objects.insert(results)

    def _parse_header(self, row):
        """
        Returns a tuple of office and party and congressional district
        if the row is a header.

        Returns (None, None, None) for a non-header row.

        Note that the district doesn't represent the district of the office
        """
        office = self._parse_office(row)
        if office:
            party = self._parse_party(row)
            district = self._parse_district(row)
        else:
            party = None
            district = None

        return office, party, district

    def _parse_office(self, row):
        for o in self.office_choices:
            if o in row[0]:
                return o

        return None

    def _parse_party(self, row):
        if 'Democratic' in row[0]:
            return 'Democratic'
        elif 'Republican' in row[0]:
            return 'Republican'
        else:
            return None

    def _parse_district(self, row):
        if 'District' not in row[0]:
            return None

        return re.search(r'(\d+)', row[0]).groups(0)[0]

    def _parse_candidates(self, row):
        candidates = []
        for col in row:
            if col != '':
                full_name = col.strip() 
                if 'Winner' in full_name:
                    # Trim winner from candidate name
                    full_name, remainder = full_name.split(' Winner')
                    winner = full_name

                candidates.append(full_name)

        return candidates, winner 
        # TODO: QUESTION: How to handle "Uncomitted to any ..." values

    def _parse_results(self, row, office, party, district, candidates,
            winner_name, common_kwargs):
        results = []
        cols = [x.strip() for x in row if x != '']
        county = cols[0].strip()
        cand_results = zip(candidates, cols[1:])

        for cand, votes in cand_results:
            result_kwargs = common_kwargs.copy()
            result_kwargs.update({
                'jurisdiction': county,
                'office': office,
                'party': party,
                'full_name': cand,
                'votes': int(votes),
            })
            if result_kwargs['primary_type'] == 'closed':
                result_kwargs['primary_party'] = party
            if office == "Representative in Congress":
                # In the case of U.S. representatives, the district represents
                # the office district.  In all other cases, it just
                # represents the level of result aggregation.
                result_kwargs['district'] = district

            if cand == winner_name:
                result_kwargs['winner'] = 'Winner'

            # Try to figure out if this is a case where results are
            # provided by congressional district split by county and
            # record this.
            result_kwargs['reporting_level'] = self._get_reporting_level(district)
            if result_kwargs['reporting_level'] == 'congressional_district_by_county':
                result_kwargs['reporting_district'] = district

            results.append(RawResult(**result_kwargs))

        return results

    def _get_reporting_level(self, district):
        """
        Returns the reporting level based on the value of the results' district.

        This deals with the way in which results for 2000 primaries are
        returned broken down by both congressional district, split by county.
        """
        if district:
            return "congressional_district_by_county"
        else:
            return "county"


class OHHTMLLoader(BaseLoader):
    """
    Loader for Ohio .aspx results files
    """
    datasource = Datasource()

    def load(self):
        table = self._get_html_table()
        rows = self._parse_html_table(table)
        winner_name = self._parse_winner_name(rows[0])
        candidate_attrs = self._parse_candidates_and_parties(rows[0],
            winner_name)
        results = self._parse_results(rows[1:88], candidate_attrs)
        RawResult.objects.insert(results)

    def _get_html_table(self):
        soup = BeautifulSoup(self._file_handle)
        return soup.find(text=re.compile("Denotes winner")).parent.parent.find_all('table')[0]

    def _parse_html_table(self, table):
        rows = []
        for tr in table.find_all('tr'):
            rows.append(self._parse_html_table_row(tr))
        return rows

    def _parse_html_table_row(self, tr):
        row = []
        cells = tr.find_all('th') + tr.find_all('td')
        for cell in cells:
            row.append(cell.text.strip())
        return row

    def _parse_winner_name(self, row):
        # winner is prefaced by an *
        name = [x for x in row if x[0] == '*'][0]
        return self._parse_name(name[1:])

    def _parse_candidates_and_parties(self, row, winner_name):
        candidate_attrs = []
        for cell in row[1:]: 
            # Skip the first cell.  It's a header, "County"
            attrs = {
                'full_name': self._parse_name(cell)
            }

            if attrs['full_name'] == winner_name:
                attrs['contest_winner'] = True

            candidate_attrs.append(attrs) 

        return candidate_attrs 

    def _parse_name(self, s):
        if s == "Other Write-Ins":
            return s

        # We know that all the candidate names are just first and last names
        bits = re.split(r'\s', s)
        return ' '.join(bits[:2])

    def _parse_party(self, s):
        if s == "Other Write-Ins":
            return None
        bits = re.split(r'\s', s)
        return bits[2]

    def _parse_write_in(self, s):
        if s == "Other Write-Ins":
            return s
        elif "Write-In" in s:
            return "Write-In"
        else:
            return "" 

    def _parse_results(self, rows, candidate_attrs):
        # These raw result attributes will be the same for every result.
        common_kwargs = self._build_common_election_kwargs()
        common_kwargs.update({
            'office': "Representative in Congress",
            'district': '4', 
            'reporting_level': "county",
        })

        results = []
        for row in rows:
            county = row[0]
            for i in range(1, len(row)):
                kwargs = common_kwargs.copy()
                kwargs.update(candidate_attrs[i-1])
                kwargs['jurisdiction'] = county
                kwargs['votes'] = self._parse_votes(row[i]) 
                results.append(RawResult(**kwargs))
        return results

    def _parse_votes(self, s):
        return int(s.split(' ')[0].replace(',', ''))
