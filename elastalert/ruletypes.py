# -*- coding: utf-8 -*-
import copy
import datetime
import sys
import time
import itertools


from sortedcontainers import SortedKeyList as sortedlist

from elastalert.util import (add_raw_postfix, dt_to_ts, EAException, elastalert_logger, elasticsearch_client,
                             format_index, get_msearch_query, hashable, kibana_adapter_client, lookup_es_key, new_get_event_ts, pretty_ts, total_seconds,
                             ts_now, ts_to_dt, expand_string_into_dict, format_string)


class RuleType(object):
    """ The base class for a rule type.
    The class must implement add_data and add any matches to self.matches.

    :param rules: A rule configuration.
    """
    required_options = frozenset()

    def __init__(self, rules, args=None):
        self.matches = []
        self.rules = rules
        self.occurrences = {}
        self.rules['category'] = self.rules.get('category', '')
        self.rules['description'] = self.rules.get('description', '')
        self.rules['owner'] = self.rules.get('owner', '')
        self.rules['priority'] = self.rules.get('priority', '2')

    def add_data(self, data):
        """ The function that the ElastAlert client calls with results from ES.
        Data is a list of dictionaries, from Elasticsearch.

        :param data: A list of events, each of which is a dictionary of terms.
        """
        raise NotImplementedError()

    def add_match(self, event):
        """ This function is called on all matching events. Rules use it to add
        extra information about the context of a match. Event is a dictionary
        containing terms directly from Elasticsearch and alerts will report
        all of the information.

        :param event: The matching event, a dictionary of terms.
        """
        # Convert datetime's back to timestamps
        ts = self.rules.get('timestamp_field')
        if ts in event:
            event[ts] = dt_to_ts(event[ts])

        self.matches.append(copy.deepcopy(event))

    def get_match_str(self, match):
        """ Returns a string that gives more context about a match.

        :param match: The matching event, a dictionary of terms.
        :return: A user facing string describing the match.
        """
        return ''

    def garbage_collect(self, timestamp):
        """ Gets called periodically to remove old data that is useless beyond given timestamp.
        May also be used to compute things in the absence of new data.

        :param timestamp: A timestamp indicating the rule has been run up to that point.
        """
        pass

    def add_count_data(self, counts):
        """ Gets called when a rule has use_count_query set to True. Called to add data from querying to the rule.

        :param counts: A dictionary mapping timestamps to hit counts.
        """
        raise NotImplementedError()

    def add_terms_data(self, terms):
        """ Gets called when a rule has use_terms_query set to True.

        :param terms: A list of buckets with a key, corresponding to query_key, and the count """
        raise NotImplementedError()

    def add_aggregation_data(self, payload):
        """ Gets called when a rule has use_terms_query set to True.
        :param terms: A list of buckets with a key, corresponding to query_key, and the count """
        raise NotImplementedError()


class CompareRule(RuleType):
    """ A base class for matching a specific term by passing it to a compare function """
    required_options = frozenset(['compound_compare_key'])

    def expand_entries(self, list_type):
        """ Expand entries specified in files using the '!file' directive, if there are
        any, then add everything to a set.
        """
        entries_set = set()
        for entry in self.rules[list_type]:
            if entry.startswith("!file"):  # - "!file /path/to/list"
                filename = entry.split()[1]
                with open(filename, 'r') as f:
                    for line in f:
                        entries_set.add(line.rstrip())
            else:
                entries_set.add(entry)
        self.rules[list_type] = entries_set

    def compare(self, event):
        """ An event is a match if this returns true """
        raise NotImplementedError()

    def add_data(self, data):
        # If compare returns true, add it as a match
        for event in data:
            if self.compare(event):
                self.add_match(event)


class BlacklistRule(CompareRule):
    """ A CompareRule where the compare function checks a given key against a blacklist """
    required_options = frozenset(['compare_key', 'blacklist'])

    def __init__(self, rules, args=None):
        super(BlacklistRule, self).__init__(rules, args=None)
        self.expand_entries('blacklist')

    def compare(self, event):
        term = lookup_es_key(event, self.rules['compare_key'])
        if term in self.rules['blacklist']:
            return True
        return False


class WhitelistRule(CompareRule):
    """ A CompareRule where the compare function checks a given term against a whitelist """
    required_options = frozenset(['compare_key', 'whitelist', 'ignore_null'])

    def __init__(self, rules, args=None):
        super(WhitelistRule, self).__init__(rules, args=None)
        self.expand_entries('whitelist')

    def compare(self, event):
        term = lookup_es_key(event, self.rules['compare_key'])
        if term is None:
            return not self.rules['ignore_null']
        if term not in self.rules['whitelist']:
            return True
        return False


class ChangeRule(CompareRule):
    """ A rule that will store values for a certain term and match if those values change """
    required_options = frozenset(['query_key', 'compound_compare_key', 'ignore_null'])
    change_map = {}
    occurrence_time = {}

    def compare(self, event):
        key = hashable(lookup_es_key(event, self.rules['query_key']))
        values = []
        elastalert_logger.debug(" Previous Values of compare keys  " + str(self.occurrences))
        for val in self.rules['compound_compare_key']:
            lookup_value = lookup_es_key(event, val)
            values.append(lookup_value)
        elastalert_logger.debug(" Current Values of compare keys   " + str(values))

        changed = False
        for val in values:
            if not isinstance(val, bool) and not val and self.rules['ignore_null']:
                return False
        # If we have seen this key before, compare it to the new value
        if key in self.occurrences:
            for idx, previous_values in enumerate(self.occurrences[key]):
                elastalert_logger.debug(" " + str(previous_values) + " " + str(values[idx]))
                changed = previous_values != values[idx]
                if changed:
                    break
            if changed:
                self.change_map[key] = (self.occurrences[key], values)
                # If using timeframe, only return true if the time delta is < timeframe
                if key in self.occurrence_time:
                    changed = event[self.rules['timestamp_field']] - self.occurrence_time[key] <= self.rules['timeframe']

        # Update the current value and time
        elastalert_logger.debug(" Setting current value of compare keys values " + str(values))
        self.occurrences[key] = values
        if 'timeframe' in self.rules:
            self.occurrence_time[key] = event[self.rules['timestamp_field']]
        elastalert_logger.debug("Final result of comparision between previous and current values " + str(changed))
        return changed

    def add_match(self, match):
        # TODO this is not technically correct
        # if the term changes multiple times before an alert is sent
        # this data will be overwritten with the most recent change
        change = self.change_map.get(hashable(lookup_es_key(match, self.rules['query_key'])))
        extra = {}
        if change:
            extra = {'old_value': change[0],
                     'new_value': change[1]}
            elastalert_logger.debug("Description of the changed records  " + str(dict(list(match.items()) + list(extra.items()))))
        super(ChangeRule, self).add_match(dict(list(match.items()) + list(extra.items())))


class FrequencyRule(RuleType):
    """ A rule that matches if num_events number of events occur within a timeframe """
    required_options = frozenset(['num_events', 'timeframe'])

    def __init__(self, *args):
        super(FrequencyRule, self).__init__(*args)
        self.ts_field = self.rules.get('timestamp_field', '@timestamp')
        self.get_ts = new_get_event_ts(self.ts_field)
        self.attach_related = self.rules.get('attach_related', False)
    
    # def add_count_data(self, data):
    #     """ Add count data to the rule. Data should be of the form {ts: count}. """
    #     if len(data) > 1:
    #         raise EAException('add_count_data can only accept one count at a time')

    #     (ts, count), = list(data.items())

    #     event = ({self.ts_field: ts}, count)
    #     self.occurrences.setdefault('all', EventWindow(self.rules['timeframe'], getTimestamp=self.get_ts)).append(event)
    #     self.check_for_match('all')

    def add_count_data(self, data):
        # data struncture should be -> data: {endtime:<dateTime>,count:<CountOfEvents>,event:[{}]}
        # if data doesn't have endtime and count as above example, raise an exception
        if not 'endtime' in data or not 'count' in data:
            raise EAException('add_count_data should have endtime and count')
        ts = data['endtime']
        count = data['count']
        doc = {}
        if 'event' in data and data['event'][0]:
            doc = data['event'][0]
        else:
            doc = {self.ts_field: ts}
        event = (doc, count)
        self.occurrences.setdefault('all', EventWindow(self.rules['timeframe'], getTimestamp=self.get_ts)).append(event)
        self.check_for_match('all')

    #nested query key optimizations
    def add_terms_data(self, terms):
        if 'nested_query_key' in self.rules and self.rules['nested_query_key'] == True:
            #letting this log message stay inorder to debug issues in future
            elastalert_logger.info(terms)
            for timestamp, buckets in terms.items():
                self.flatten_nested_aggregations(timestamp,buckets)
        else:
            for timestamp, buckets in terms.items():
                for bucket in buckets:
                    event = ({self.ts_field: timestamp,
                            self.rules['query_key']: bucket['key']}, bucket['doc_count'])
                    self.occurrences.setdefault(bucket['key'], EventWindow(self.rules['timeframe'], getTimestamp=self.get_ts)).append(event)
                    self.check_for_match(bucket['key'])

    #nested query key optimizations
    def flatten_nested_aggregations(self,timestamp,buckets,key=None):
        for bucket in buckets:
            if key == None:
                nestedkey = str(bucket['key'])
            else:
                nestedkey = key + ',' + str(bucket['key'])
            if 'counts' in bucket:
                self.flatten_nested_aggregations(timestamp,bucket['counts']['buckets'],nestedkey)
            else:
                event = ({self.ts_field: timestamp,
                        self.rules['query_key']: nestedkey}, bucket['doc_count'])
                self.occurrences.setdefault(nestedkey, EventWindow(self.rules['timeframe'], getTimestamp=self.get_ts)).append(event)
                self.check_for_match(nestedkey)


    def add_data(self, data):
        if 'query_key' in self.rules:
            qk = self.rules['query_key']
        else:
            qk = None

        for event in data:
            if qk:
                key = hashable(lookup_es_key(event, qk))
            else:
                # If no query_key, we use the key 'all' for all events
                key = 'all'

            # Store the timestamps of recent occurrences, per key
            self.occurrences.setdefault(key, EventWindow(self.rules['timeframe'], getTimestamp=self.get_ts)).append((event, 1))
            self.check_for_match(key, end=False)

        # We call this multiple times with the 'end' parameter because subclasses
        # may or may not want to check while only partial data has been added
        if key in self.occurrences:  # could have been emptied by previous check
            self.check_for_match(key, end=True)

    def check_for_match(self, key, end=False):
        # Match if, after removing old events, we hit num_events.
        # the 'end' parameter depends on whether this was called from the
        # middle or end of an add_data call and is used in subclasses
        if self.occurrences[key].count() >= self.rules['num_events']:
            event = self.occurrences[key].data[-1][0]
            if self.attach_related:
                event['related_events'] = [data[0] for data in self.occurrences[key].data[:-1]]
            self.add_match(event)
            self.occurrences.pop(key)

    def garbage_collect(self, timestamp):
        """ Remove all occurrence data that is beyond the timeframe away """
        stale_keys = []
        for key, window in self.occurrences.items():
            if timestamp - lookup_es_key(window.data[-1][0], self.ts_field) > self.rules['timeframe']:
                stale_keys.append(key)
        list(map(self.occurrences.pop, stale_keys))

    def get_match_str(self, match):
        lt = self.rules.get('use_local_time')
        fmt = self.rules.get('custom_pretty_ts_format')
        match_ts = lookup_es_key(match, self.ts_field)
        starttime = pretty_ts(dt_to_ts(ts_to_dt(match_ts) - self.rules['timeframe']), lt, fmt)
        endtime = pretty_ts(match_ts, lt, fmt)
        message = 'At least %d events occurred between %s and %s\n\n' % (self.rules['num_events'],
                                                                         starttime,
                                                                         endtime)
        return message


class AnyRule(RuleType):
    """ A rule that will match on any input data """

    def add_data(self, data):
        for datum in data:
            self.add_match(datum)


class EventWindow(object):
    """ A container for hold event counts for rules which need a chronological ordered event window. """

    def __init__(self, timeframe, onRemoved=None, getTimestamp=new_get_event_ts('@timestamp')):
        self.timeframe = timeframe
        self.onRemoved = onRemoved
        self.get_ts = getTimestamp
        self.data = sortedlist(key=self.get_ts)
        self.running_count = 0

    def clear(self):
        self.data = sortedlist(key=self.get_ts)
        self.running_count = 0

    def append(self, event):
        """ Add an event to the window. Event should be of the form (dict, count).
        This will also pop the oldest events and call onRemoved on them until the
        window size is less than timeframe. """
        self.data.add(event)
        if event and event[1]:
            self.running_count += event[1]
        
        while self.duration() >= self.timeframe:
            oldest = self.data[0]
            self.data.remove(oldest)
            if oldest and oldest[1]:
                self.running_count -= oldest[1]
            self.onRemoved and self.onRemoved(oldest)

    def duration(self):
        """ Get the size in timedelta of the window. """
        if not self.data:
            return datetime.timedelta(0)
        return self.get_ts(self.data[-1]) - self.get_ts(self.data[0])

    def count(self):
        """ Count the number of events in the window. """
        return self.running_count

    def mean(self):
        """ Compute the mean of the value_field in the window. """
        if len(self.data) > 0:
            datasum = 0
            datalen = 0
            for dat in self.data:
                if "placeholder" not in dat[0]:
                    datasum += dat[1]
                    datalen += 1
            if datalen > 0:
                return datasum / float(datalen)
            return None
        else:
            return None

    def min(self):
        """ The minimum of the value_field in the window. """
        if len(self.data) > 0:
            return min([x[1] for x in self.data])
        else:
            return None

    def max(self):
        """ The maximum of the value_field in the window. """
        if len(self.data) > 0:
            return max([x[1] for x in self.data])
        else:
            return None

    def __iter__(self):
        return iter(self.data)

    def append_middle(self, event):
        """ Attempt to place the event in the correct location in our deque.
        Returns True if successful, otherwise False. """
        rotation = 0
        ts = self.get_ts(event)

        # Append left if ts is earlier than first event
        if self.get_ts(self.data[0]) > ts:
            self.data.appendleft(event)
            if event and event[1]:
                self.running_count += event[1]
            return

        # Rotate window until we can insert event
        while self.get_ts(self.data[-1]) > ts:
            self.data.rotate(1)
            rotation += 1
            if rotation == len(self.data):
                # This should never happen
                return
        self.data.append(event)
        if event and event[1]:
            self.running_count += event[1]
        self.data.rotate(-rotation)

class TermsWindow:

    """ For each field configured in new_term rule, This term window is created and maintained.
    A sliding window is maintained and count of all the existing terms are stored.
    
    data - Sliding window which holds the queried terms and counts along with timestamp. This list is sorted in ascending order based on the timestamp
    existing_terms - A set containing existing terms. mainly used for looking up new terms.
    new_terms - Dictionary of EventWindows created for new terms.
    count_dict - Dictionary containing the count of existing terms. When something is added to or popped from the sliding window - data, this count is updated
    """
    def __init__(self, term_window_size, ts_field , threshold, threshold_window_size, get_ts):
        self.term_window_size = term_window_size
        self.ts_field = ts_field
        self.threshold = threshold
        self.threshold_window_size = threshold_window_size
        self.get_ts = get_ts

        self.data = sortedlist(key= lambda x: x[0]) #sorted by timestamp
        self.existing_terms = set()
        self.potential_new_term_windows = {}
        self.count_dict = {}

    """ used to add new terms and their counts for a timestamp into the sliding window - data """
    def add(self, timestamp, terms, counts):
        for (term, count) in zip(terms, counts):
            if term not in self.count_dict:
                self.count_dict[term] = 0    
            self.count_dict[term] += count
            self.existing_terms.add(term)
        self.data.add((timestamp, terms,counts))
        self.resize()

    """ function to split new terms and existing terms when given timestamp, terms and counts"""
    def split(self,timestamp, terms, counts):
        unseen_terms = []
        unseen_counts = []
        seen_terms = []
        seen_counts = []
        self.resize(till = timestamp - self.term_window_size) 
        for (term, count) in zip(terms, counts):
            if term not in self.existing_terms:
                unseen_terms.append(term)
                unseen_counts.append(count)
            else:
                seen_terms.append(term)
                seen_counts.append(count)
        return seen_terms, seen_counts, unseen_terms, unseen_counts

    """ function to update the potential new terms windows"""
    def update_potential_new_term_windows(self, timestamp, unseen_terms, unseen_counts):
        for (term, count) in zip(unseen_terms, unseen_counts):
            event = ({self.ts_field: timestamp}, count)
            window = self.potential_new_term_windows.setdefault( term , EventWindow(self.threshold_window_size, getTimestamp=self.get_ts))
            window.append(event)


    """function to get the matched new_terms that have crossed the threshold configured"""
    def extract_new_terms(self, potential_new_terms, potential_term_counts):
        new_terms = []
        new_counts = []
        for (potential_new_term, potential_term_count) in zip(potential_new_terms, potential_term_counts):
            window = self.potential_new_term_windows.get(potential_new_term)
            if window.count() >= self.threshold:
                new_terms.append(potential_new_term)
                new_counts.append(potential_term_count)
                self.potential_new_term_windows.pop(potential_new_term)
        return new_terms, new_counts

    def get_new_terms(self, timestamp, terms, counts):
        existing_terms, existing_counts, potential_new_terms, potential_term_counts = self.split(timestamp, terms, counts) # Split the potential_new_terms and existing terms along with their counts based on current timestamp
        self.update_potential_new_term_windows(timestamp, potential_new_terms, potential_term_counts) # Update the potential_new_term_windows
        new_terms, new_counts = self.extract_new_terms( potential_new_terms, potential_term_counts) # extract and delete new terms from the potential_new_terms_window. 
        self.add(timestamp, existing_terms + new_terms, existing_counts + new_counts) # Add the exiting terms and new_terms to the terms_window
        return new_terms, new_counts
            
        
    """ This fn makes sure that the duration of the sliding window does not exceed term_window_size
    all the events with their timestamp lesser than 'till' are popped and the counts of keys in popped events are subtracted from count_dict
    After subtraction, if a term's count reaches 0, they are removed from count_dict and existing_terms, i.e they have not occured in terms_window duration
    by default, till =  (last event's timestamp - term_window_size  ) , 
    """
    def resize(self, till=None):
        if len(self.data)==0:
            return

        if till == None:
            till = self.data[-1][0] - self.term_window_size

        while len(self.data)!=0 and self.data[0][0] < till:
            timestamp, keys, counts = self.data.pop(0)
            for i in range(len(keys)):
                self.count_dict[keys[i]] -= counts[i]
                if self.count_dict[keys[i]] <= 0:
                    self.count_dict.pop(keys[i])
                    self.existing_terms.discard(keys[i])

    


class SpikeRule(RuleType):
    """ A rule that uses two sliding windows to compare relative event frequency. """
    required_options = frozenset(['timeframe', 'spike_height', 'spike_type'])

    def __init__(self, *args):
        super(SpikeRule, self).__init__(*args)
        self.timeframe = self.rules['timeframe']

        self.ref_windows = {}
        self.cur_windows = {}

        self.ts_field = self.rules.get('timestamp_field', '@timestamp')
        self.get_ts = new_get_event_ts(self.ts_field)
        self.first_event = {}
        self.skip_checks = {}

        self.field_value = self.rules.get('field_value')

        self.ref_window_filled_once = False

    def add_count_data(self, data):
        #""" Add count data to the rule. Data should be of the form {ts: count}. """
        # if len(data) > 1:
        #     raise EAException('add_count_data can only accept one count at a time')
        # for ts, count in data.items():
        #     self.handle_event({self.ts_field: ts}, count, 'all')

        # data struncture should be -> data: {endtime:<dateTime>,count:<CountOfEvents>,event:[{}]}
        # if data doesn't have endtime and count as above example, raise an exception
        ts = data['endtime']
        count = data['count']
        self.handle_event({self.ts_field: ts}, count, 'all')     

    def add_terms_data(self, terms):
        for timestamp, buckets in terms.items():
            for bucket in buckets:
                count = bucket['doc_count']
                event = {self.ts_field: timestamp,
                         self.rules['query_key']: bucket['key']}
                key = bucket['key']
                self.handle_event(event, count, key)

    def add_data(self, data):
        for event in data:
            qk = self.rules.get('query_key', 'all')
            if qk != 'all':
                qk = hashable(lookup_es_key(event, qk))
                if qk is None:
                    qk = 'other'
            if self.field_value is not None:
                if self.field_value in event:
                    count = lookup_es_key(event, self.field_value)
                    if count is not None:
                        try:
                            count = int(count)
                        except ValueError:
                            elastalert_logger.warn('{} is not a number: {}'.format(self.field_value, count))
                        else:
                            self.handle_event(event, count, qk)
            else:
                self.handle_event(event, 1, qk)

    def get_spike_values(self, qk):
        """
        extending ref/cur value retrieval logic for spike aggregations
        """
        spike_check_type = self.rules.get('metric_agg_type')
        if spike_check_type in [None, 'sum', 'value_count', 'cardinality', 'percentile']:
            # default count logic is appropriate in all these cases
            return self.ref_windows[qk].count(), self.cur_windows[qk].count()
        elif spike_check_type == 'avg':
            return self.ref_windows[qk].mean(), self.cur_windows[qk].mean()
        elif spike_check_type == 'min':
            return self.ref_windows[qk].min(), self.cur_windows[qk].min()
        elif spike_check_type == 'max':
            return self.ref_windows[qk].max(), self.cur_windows[qk].max()

    def clear_windows(self, qk, event):
        # Reset the state and prevent alerts until windows filled again
        self.ref_windows[qk].clear()
        self.first_event.pop(qk)
        self.skip_checks[qk] = lookup_es_key(event, self.ts_field) + self.rules['timeframe'] * 2

    def handle_event(self, event, count, qk='all'):
        self.first_event.setdefault(qk, event)

        self.ref_windows.setdefault(qk, EventWindow(self.timeframe, getTimestamp=self.get_ts))
        self.cur_windows.setdefault(qk, EventWindow(self.timeframe, self.ref_windows[qk].append, self.get_ts))

        self.cur_windows[qk].append((event, count))

        # Don't alert if ref window has not yet been filled for this key AND
        if lookup_es_key(event, self.ts_field) - self.first_event[qk][self.ts_field] < self.rules['timeframe'] * 2:
            # ElastAlert has not been running long enough for any alerts OR
            if not self.ref_window_filled_once:
                return
            # This rule is not using alert_on_new_data (with query_key) OR
            if not (self.rules.get('query_key') and self.rules.get('alert_on_new_data')):
                return
            # An alert for this qk has recently fired
            if qk in self.skip_checks and lookup_es_key(event, self.ts_field) < self.skip_checks[qk]:
                return
        else:
            self.ref_window_filled_once = True

        if self.field_value is not None:
            if self.find_matches(self.ref_windows[qk].mean(), self.cur_windows[qk].mean()):
                # skip over placeholder events
                for match, count in self.cur_windows[qk].data:
                    if "placeholder" not in match:
                        break
                self.add_match(match, qk)
                self.clear_windows(qk, match)
        else:
            ref, cur = self.get_spike_values(qk)
            if self.find_matches(ref, cur):
                # skip over placeholder events which have count=0
                for match, count in self.cur_windows[qk].data:
                    if count:
                        break

                self.add_match(match, qk)
                self.clear_windows(qk, match)

    def add_match(self, match, qk):
        extra_info = {}
        if self.field_value is None:
            reference_count, spike_count = self.get_spike_values(qk)
        else:
            spike_count = self.cur_windows[qk].mean()
            reference_count = self.ref_windows[qk].mean()
        extra_info = {'spike_count': spike_count,
                      'reference_count': reference_count}

        match = dict(list(match.items()) + list(extra_info.items()))

        super(SpikeRule, self).add_match(match)

    def find_matches(self, ref, cur):
        """ Determines if an event spike or dip happening. """
        # Apply threshold limits
        if self.field_value is None:
            if (cur < self.rules.get('threshold_cur', 0) or
                    ref < self.rules.get('threshold_ref', 0)):
                return False
        elif ref is None or ref == 0 or cur is None or cur == 0:
            return False

        spike_up, spike_down = False, False
        if cur <= ref / self.rules['spike_height']:
            spike_down = True
        if cur >= ref * self.rules['spike_height']:
            spike_up = True

        if (self.rules['spike_type'] in ['both', 'up'] and spike_up) or \
           (self.rules['spike_type'] in ['both', 'down'] and spike_down):
            return True
        return False

    def get_match_str(self, match):
        if self.field_value is None:
            message = 'An abnormal number (%d) of events occurred around %s.\n' % (
                match['spike_count'],
                pretty_ts(match[self.rules['timestamp_field']], self.rules.get('use_local_time'), self.rules.get('custom_pretty_ts_format'))
            )
            message += 'Preceding that time, there were only %d events within %s\n\n' % (match['reference_count'], self.rules['timeframe'])
        else:
            message = 'An abnormal average value (%.2f) of field \'%s\' occurred around %s.\n' % (
                match['spike_count'],
                self.field_value,
                pretty_ts(match[self.rules['timestamp_field']],
                          self.rules.get('use_local_time'),
                          self.rules.get('custom_pretty_ts_format'))
            )
            message += 'Preceding that time, the field had an average value of (%.2f) within %s\n\n' % (
                match['reference_count'], self.rules['timeframe'])
        return message

    def garbage_collect(self, ts):
        # Windows are sized according to their newest event
        # This is a placeholder to accurately size windows in the absence of events
        for qk in list(self.cur_windows.keys()):
            # If we havn't seen this key in a long time, forget it
            if qk != 'all' and self.ref_windows[qk].count() == 0 and self.cur_windows[qk].count() == 0:
                self.cur_windows.pop(qk)
                self.ref_windows.pop(qk)
                continue
            placeholder = {self.ts_field: ts, "placeholder": True}
            # The placeholder may trigger an alert, in which case, qk will be expected
            if qk != 'all':
                placeholder.update({self.rules['query_key']: qk})
            self.handle_event(placeholder, 0, qk)

class AdvanceSearchRule(RuleType):
    """ A rule that uses a query_string query to perform a advanced search like parsing, evaluating conditions, calculating aggs etc """
    required_options = frozenset(['alert_field'])

    def __init__(self, *args):
        super(AdvanceSearchRule, self).__init__(*args)
        if 'max_threshold' not in self.rules and 'min_threshold' not in self.rules:
            raise EAException("AdvanceSearchRule must have one of either max_threshold or min_threshold")
        #self.query_string = self.rules.get('query_string')
        self.rules['aggregation_query_element'] = {"query": ""}

    def run_query(self):
        # Implement the logic to run the query using the query_string
        raise NotImplementedError()

    def add_aggregation_data(self, payload):
        for timestamp, payload_data in payload.items():
            self.check_matches(payload_data,timestamp)
    
    def check_matches(self,data,timestamp):
        #results=[]
        for key, value in data.items():
            if 'buckets' in value:
                if len(value['buckets']) >=0 :
                    self.check_matches_recursive(key,value['buckets'],timestamp)
            else:
                if self.crossed_thresholds(value['value']):
                    match={"key":key,"value":value['value'],self.rules['timestamp_field']:timestamp}
                    self.add_match(match)

    def check_matches_recursive(self,top_level_key,buckets,timestamp,key_prefix=''):
        key = top_level_key
        if len(data_value) > 0:
            for data in buckets:
                local_key_prefix = key_prefix
                key_prefix = key_prefix+ ','+ data['key'] if key_prefix else data['key']
                for k,v in data.items():
                    if k!= 'key' and k!= 'doc_count':
                        if 'buckets' in v:
                            local_key = key
                            key = key + ','+k
                            self.check_matches_recursive(key,v['buckets'],key_prefix)
                            key = local_key
                        else:
                            if self.rules['alert_field'] in k:
                                if self.crossed_thresholds(v['value']):
                                    match={"key": key,"value":v['value'],"key_value":key_prefix,self.rules['timestamp_field']: timestamp}
                                    self.add_match(match)
                key_prefix = local_key_prefix
    
    def crossed_thresholds(self, metric_value):
        if metric_value is None:
            return False
        if 'max_threshold' in self.rules and float(metric_value) > self.rules['max_threshold']:
            return True
        if 'min_threshold' in self.rules and float(metric_value) < self.rules['min_threshold']:
            return True
        return False
    

class FlatlineRule(FrequencyRule):
    """ A rule that matches when there is a low number of events given a timeframe. """
    required_options = frozenset(['timeframe', 'threshold'])

    def __init__(self, *args):
        super(FlatlineRule, self).__init__(*args)
        self.threshold = self.rules['threshold']

        # Dictionary mapping query keys to the first events
        self.first_event = {}

    def check_for_match(self, key, end=True):
        # This function gets called between every added document with end=True after the last
        # We ignore the calls before the end because it may trigger false positives
        if not end:
            return

        most_recent_ts = self.get_ts(self.occurrences[key].data[-1])
        if self.first_event.get(key) is None:
            self.first_event[key] = most_recent_ts

        # Don't check for matches until timeframe has elapsed
        if most_recent_ts - self.first_event[key] < self.rules['timeframe']:
            return

        # Match if, after removing old events, we hit num_events
        count = self.occurrences[key].count()
        if count < self.rules['threshold']:
            # Do a deep-copy, otherwise we lose the datetime type in the timestamp field of the last event
            event = copy.deepcopy(self.occurrences[key].data[-1][0])
            event.update(key=key, count=count)
            event[self.rules['query_key']]=key
            self.add_match(event)

            if not self.rules.get('forget_keys'):
                # After adding this match, leave the occurrences windows alone since it will
                # be pruned in the next add_data or garbage_collect, but reset the first_event
                # so that alerts continue to fire until the threshold is passed again.
                least_recent_ts = self.get_ts(self.occurrences[key].data[0])
                timeframe_ago = most_recent_ts - self.rules['timeframe']
                self.first_event[key] = min(least_recent_ts, timeframe_ago)
            else:
                # Forget about this key until we see it again
                self.first_event.pop(key)
                self.occurrences.pop(key)

    def get_match_str(self, match):
        ts = match[self.rules['timestamp_field']]
        lt = self.rules.get('use_local_time')
        fmt = self.rules.get('custom_pretty_ts_format')
        message = 'An abnormally low number of events occurred around %s.\n' % (pretty_ts(ts, lt, fmt))
        message += 'Between %s and %s, there were less than %s events.\n\n' % (
            pretty_ts(dt_to_ts(ts_to_dt(ts) - self.rules['timeframe']), lt, fmt),
            pretty_ts(ts, lt, fmt),
            self.rules['threshold']
        )
        return message

    def garbage_collect(self, ts):
        # We add an event with a count of zero to the EventWindow for each key. This will cause the EventWindow
        # to remove events that occurred more than one `timeframe` ago, and call onRemoved on them.
        default = ['all'] if 'query_key' not in self.rules else []
        for key in list(self.occurrences.keys()) or default:
            self.occurrences.setdefault(
                key,
                EventWindow(self.rules['timeframe'], getTimestamp=self.get_ts)
            ).append(
                ({self.ts_field: ts}, 0)
            )
            self.first_event.setdefault(key, ts)
            self.check_for_match(key)


class NewTermsRule(RuleType):
    """ Alerts on a new value in a list of fields. """

    def __init__(self, rule, args=None):
        super(NewTermsRule, self).__init__(rule, args)
        self.term_windows = {}
        self.last_updated_at = None
        self.es = kibana_adapter_client(self.rules)
        self.ts_field = self.rules.get('timestamp_field', '@timestamp')
        self.get_ts = new_get_event_ts(self.ts_field)
        self.new_terms = {}
        
        self.threshold = rule.get('threshold',0)

        # terms_window_size : Default & Upperbound - 7 Days
        self.window_size = min(datetime.timedelta(**self.rules.get('terms_window_size', {'days': 7})), datetime.timedelta(**{'days': 7}))
        
        self.step =  datetime.timedelta(**{'hours': 1})
        
        # terms_size : Default - 500, Upperbound: 1000
        self.terms_size = min(self.rules.get('terms_size', 500),1000)

        # threshold_window_size
        self.threshold_window_size =  min( datetime.timedelta(**self.rules.get('threshold_window_size', {'hours': 1})), datetime.timedelta(**{'days': 2}) )

        # Allow the use of query_key or fields
        if 'fields' not in self.rules:
            if 'query_key' not in self.rules:
                raise EAException("fields or query_key must be specified")
            self.fields = self.rules['query_key']
        else:
            self.fields = self.rules['fields']
        if not self.fields:
            raise EAException("fields must not be an empty list")
        if type(self.fields) != list:
            self.fields = [self.fields]
        if self.rules.get('use_terms_query') and \
                (len(self.fields) != 1 or (len(self.fields) == 1 and type(self.fields[0]) == list)):
            raise EAException("use_terms_query can only be used with a single non-composite field")
        if self.rules.get('use_terms_query'):
            if [self.rules['query_key']] != self.fields:
                raise EAException('If use_terms_query is specified, you cannot specify different query_key and fields')
            if not self.rules.get('query_key').endswith('.keyword') and not self.rules.get('query_key').endswith('.raw'):
                if self.rules.get('use_keyword_postfix', False): # making it false by default as we wont use the keyword suffix
                    elastalert_logger.warn('Warning: If query_key is a non-keyword field, you must set '
                                           'use_keyword_postfix to false, or add .keyword/.raw to your query_key.')
        try:
            self.get_all_terms(args=args)
        except Exception as e:
            # Refuse to start if we cannot get existing terms
            raise EAException('Error searching for existing terms: %s' % (repr(e))).with_traceback(sys.exc_info()[2])
        
        

    def get_new_term_query(self,starttime,endtime,field):
        
        field_name = {
            "field": "",
            "size": self.terms_size,
            "order": {
                "_count": "desc"
            }
        }  
        
        query = {
            "aggs": {
                "values": {
                    "terms": field_name
                }
            }
        }

        query["query"] = {
            'bool': {
                'filter': {
                    'bool': {
                        'must': [{
                            'range': {
                                self.rules['timestamp_field']: {
                                    'lt': self.rules['dt_to_ts'](endtime),
                                    'gte': self.rules['dt_to_ts'](starttime)
                                }
                            }
                        }]
                    }
                }
            }
        }

        filter_level = query['query']['bool']['filter']['bool']['must']
        if 'filter' in self.rules:
            for item in self.rules['filter']:
                if "query" in item:
                    filter_level.append(item['query'])
                else:
                    filter_level.append(item)

        # For composite keys, we will need to perform sub-aggregations
        if type(field) == list:
            self.term_windows.setdefault(tuple(field), TermsWindow(self.window_size, self.ts_field , self.threshold, self.threshold_window_size, self.get_ts))
            level = query['aggs']
            # Iterate on each part of the composite key and add a sub aggs clause to the elastic search query
            for i, sub_field in enumerate(field):
                if self.rules.get('use_keyword_postfix', False): # making it false by default as we wont use the keyword suffix
                    level['values']['terms']['field'] = add_raw_postfix(sub_field, True)
                else:
                    level['values']['terms']['field'] = sub_field
                if i < len(field) - 1:
                    # If we have more fields after the current one, then set up the next nested structure
                    level['values']['aggs'] = {'values': {'terms': copy.deepcopy(field_name)}}
                    level = level['values']['aggs']
        else:
            self.term_windows.setdefault(field, TermsWindow(self.window_size, self.ts_field , self.threshold, self.threshold_window_size, self.get_ts))
            # For non-composite keys, only a single agg is needed
            if self.rules.get('use_keyword_postfix', False):# making it false by default as we wont use the keyword suffix
                field_name['field'] = add_raw_postfix(field, True)
            else:
                field_name['field'] = field

        return query

    def get_terms_data(self, es, starttime, endtime, field, request_timeout= None):
        terms = []
        counts = []
        query = self.get_new_term_query(starttime,endtime,field)
        request = get_msearch_query(query,self.rules)
        
        if request_timeout == None:
            res = es.msearch(body=request) 
        else:
            res = es.msearch(body=request, request_timeout=request_timeout)
        res = res['responses'][0] 

        if 'aggregations' in res:
            buckets = res['aggregations']['values']['buckets']
            if type(field) == list:
                for bucket in buckets:
                    keys, doc_counts = self.flatten_aggregation_hierarchy(bucket)
                    terms += keys
                    counts += doc_counts
            else:
                for bucket in buckets:
                    terms.append(bucket['key'])
                    counts.append(bucket['doc_count'])

        return terms, counts




    def get_all_terms(self,args):
        """ Performs a terms aggregation for each field to get every existing term. """

        if args and hasattr(args, 'start') and args.start:
            end = ts_to_dt(args.start)
        elif 'start_date' in self.rules:
            end = ts_to_dt(self.rules['start_date'])
        else:
            end = ts_now()

        start = end - self.window_size
        
        for field in self.fields:
            tmp_start = start
            
            # Query the entire time range in small chunks
            while tmp_start < end:
                tmp_end = min(tmp_start + self.step, end)
                terms, counts = self.get_terms_data(self.es, tmp_start, tmp_end, field, request_timeout=50)
                self.term_windows[self.get_lookup_key(field)].add(tmp_end,terms,counts)
                tmp_start = tmp_end
                

            for lookup_key, window in self.term_windows.items():
                if not window.existing_terms:
                    if type(lookup_key) == tuple:
                        # If we don't have any results, it could either be because of the absence of any baseline data
                        # OR it may be because the composite key contained a non-primitive type.  Either way, give the
                        # end-users a heads up to help them debug what might be going on.
                        elastalert_logger.warning((
                            'No results were found from all sub-aggregations.  This can either indicate that there is '
                            'no baseline data OR that a non-primitive field was used in a composite key.'
                        ))
                    else:
                        elastalert_logger.info('Found no values for %s' % (field))
                    continue
                elastalert_logger.info('Found %s unique values for %s' % (len(window.existing_terms), lookup_key))
        # self.last_updated_at = ts_now()

    def flatten_aggregation_hierarchy(self, root, hierarchy_tuple=()):
        """ For nested aggregations, the results come back in the following format:
            {
            "aggregations" : {
                "filtered" : {
                  "doc_count" : 37,
                  "values" : {
                    "doc_count_error_upper_bound" : 0,
                    "sum_other_doc_count" : 0,
                    "buckets" : [ {
                      "key" : "1.1.1.1", # IP address (root)
                      "doc_count" : 13,
                      "values" : {
                        "doc_count_error_upper_bound" : 0,
                        "sum_other_doc_count" : 0,
                        "buckets" : [ {
                          "key" : "80",    # Port (sub-aggregation)
                          "doc_count" : 3,
                          "values" : {
                            "doc_count_error_upper_bound" : 0,
                            "sum_other_doc_count" : 0,
                            "buckets" : [ {
                              "key" : "ack",  # Reason (sub-aggregation, leaf-node)
                              "doc_count" : 3
                            }, {
                              "key" : "syn",  # Reason (sub-aggregation, leaf-node)
                              "doc_count" : 1
                            } ]
                          }
                        }, {
                          "key" : "82",    # Port (sub-aggregation)
                          "doc_count" : 3,
                          "values" : {
                            "doc_count_error_upper_bound" : 0,
                            "sum_other_doc_count" : 0,
                            "buckets" : [ {
                              "key" : "ack",  # Reason (sub-aggregation, leaf-node)
                              "doc_count" : 3
                            }, {
                              "key" : "syn",  # Reason (sub-aggregation, leaf-node)
                              "doc_count" : 3
                            } ]
                          }
                        } ]
                      }
                    }, {
                      "key" : "2.2.2.2", # IP address (root)
                      "doc_count" : 4,
                      "values" : {
                        "doc_count_error_upper_bound" : 0,
                        "sum_other_doc_count" : 0,
                        "buckets" : [ {
                          "key" : "443",    # Port (sub-aggregation)
                          "doc_count" : 3,
                          "values" : {
                            "doc_count_error_upper_bound" : 0,
                            "sum_other_doc_count" : 0,
                            "buckets" : [ {
                              "key" : "ack",  # Reason (sub-aggregation, leaf-node)
                              "doc_count" : 3
                            }, {
                              "key" : "syn",  # Reason (sub-aggregation, leaf-node)
                              "doc_count" : 3
                            } ]
                          }
                        } ]
                      }
                    } ]
                  }
                }
              }
            }

            Each level will either have more values and buckets, or it will be a leaf node
            We'll ultimately return a flattened list with the hierarchies appended as strings,
            e.g the above snippet would yield a list with:

            [
             ('1.1.1.1', '80', 'ack'),
             ('1.1.1.1', '80', 'syn'),
             ('1.1.1.1', '82', 'ack'),
             ('1.1.1.1', '82', 'syn'),
             ('2.2.2.2', '443', 'ack'),
             ('2.2.2.2', '443', 'syn')
            ]

            A similar formatting will be performed in the add_data method and used as the basis for comparison

        """
        final_keys = []
        final_counts = []
        # There are more aggregation hierarchies left.  Traverse them.
        if 'values' in root:
            keys, counts = self.flatten_aggregation_hierarchy(root['values']['buckets'], hierarchy_tuple + (root['key'],))
            final_keys += keys
            final_counts += counts
        else:
            # We've gotten to a sub-aggregation, which may have further sub-aggregations
            # See if we need to traverse further
            for node in root:
                if 'values' in node:
                    keys, counts = self.flatten_aggregation_hierarchy(node, hierarchy_tuple)
                    final_keys += keys
                    final_counts += counts
                else:
                    final_keys.append(hierarchy_tuple + (node['key'],))
                    final_counts.append(node['doc_count'])
        return final_keys, final_counts

    def add_terms_data(self, payload):
        timestamp = list(payload.keys())[0]
        data = payload[timestamp]
        for field in self.fields:
            lookup_key = self.get_lookup_key(field)
            keys, counts =  data[lookup_key]

            new_terms, new_counts = self.term_windows[lookup_key].get_new_terms(timestamp, keys, counts )
            
            # append and get all match keys and counts
            for (new_term, new_count) in zip(new_terms, new_counts):
                match = {
                    "field": lookup_key,
                    self.rules['timestamp_field']: timestamp,
                    "new_value": tuple(new_term) if type(new_term) == list else new_term,
                    "hits" : new_count
                    }
                self.add_match(copy.deepcopy(match))

    ### NOT USED ANYMORE ###      
    # def add_data(self, data):
    #     for document in data:
    #         for field in self.fields:
    #             value = ()
    #             lookup_field = field
    #             if type(field) == list:
    #                 # For composite keys, make the lookup based on all fields
    #                 # Make it a tuple since it can be hashed and used in dictionary lookups
    #                 lookup_field = tuple(field)
    #                 for sub_field in field:
    #                     lookup_result = lookup_es_key(document, sub_field)
    #                     if not lookup_result:
    #                         value = None
    #                         break
    #                     value += (lookup_result,)
    #             else:
    #                 value = lookup_es_key(document, field)
    #             if not value and self.rules.get('alert_on_missing_field'):
    #                 document['missing_field'] = lookup_field
    #                 self.add_match(copy.deepcopy(document))
    #             elif value:
    #                 if value not in self.seen_values[lookup_field]:
    #                     document['new_field'] = lookup_field
    #                     self.add_match(copy.deepcopy(document))
    #                     self.seen_values[lookup_field].append(value)

    ### NOT USED ANYMORE ###      
    # def add_terms_data(self, terms):
    #     # With terms query, len(self.fields) is always 1 and the 0'th entry is always a string
    #     field = self.fields[0]
    #     for timestamp, buckets in terms.items():
    #         for bucket in buckets:
    #             if bucket['doc_count']:
    #                 if bucket['key'] not in self.seen_values[field]:
    #                     match = {field: bucket['key'],
    #                              self.rules['timestamp_field']: timestamp,
    #                              'new_field': field}
    #                     self.add_match(match)
    #                     self.seen_values[field].append(bucket['key'])

    def get_lookup_key(self,field):
        return tuple(field) if type(field) == list else field


class CardinalityRule(RuleType):
    """ A rule that matches if cardinality of a field is above or below a threshold within a timeframe """
    required_options = frozenset(['timeframe', 'cardinality_field'])

    def __init__(self, *args):
        super(CardinalityRule, self).__init__(*args)
        if 'max_cardinality' not in self.rules and 'min_cardinality' not in self.rules:
            raise EAException("CardinalityRule must have one of either max_cardinality or min_cardinality")
        self.ts_field = self.rules.get('timestamp_field', '@timestamp')
        self.cardinality_field = self.rules['cardinality_field']
        self.cardinality_cache = {}
        self.first_event = {}
        self.timeframe = self.rules['timeframe']

    def add_data(self, data):
        qk = self.rules.get('query_key')
        for event in data:
            if qk:
                key = hashable(lookup_es_key(event, qk))
            else:
                # If no query_key, we use the key 'all' for all events
                key = 'all'
            self.cardinality_cache.setdefault(key, {})
            self.first_event.setdefault(key, lookup_es_key(event, self.ts_field))
            value = hashable(lookup_es_key(event, self.cardinality_field))
            if value is not None:
                # Store this timestamp as most recent occurence of the term
                self.cardinality_cache[key][value] = lookup_es_key(event, self.ts_field)
                self.check_for_match(key, event)

    def check_for_match(self, key, event, gc=True):
        # Check to see if we are past max/min_cardinality for a given key
        time_elapsed = lookup_es_key(event, self.ts_field) - self.first_event.get(key, lookup_es_key(event, self.ts_field))
        timeframe_elapsed = time_elapsed > self.timeframe
        if (len(self.cardinality_cache[key]) > self.rules.get('max_cardinality', float('inf')) or
                (len(self.cardinality_cache[key]) < self.rules.get('min_cardinality', float('-inf')) and timeframe_elapsed)):
            # If there might be a match, run garbage collect first, as outdated terms are only removed in GC
            # Only run it if there might be a match so it doesn't impact performance
            if gc:
                self.garbage_collect(lookup_es_key(event, self.ts_field))
                self.check_for_match(key, event, False)
            else:
                self.first_event.pop(key, None)
                self.add_match(event)

    def garbage_collect(self, timestamp):
        """ Remove all occurrence data that is beyond the timeframe away """
        for qk, terms in list(self.cardinality_cache.items()):
            for term, last_occurence in list(terms.items()):
                if timestamp - last_occurence > self.rules['timeframe']:
                    self.cardinality_cache[qk].pop(term)

            # Create a placeholder event for if a min_cardinality match occured
            if 'min_cardinality' in self.rules:
                event = {self.ts_field: timestamp}
                if 'query_key' in self.rules:
                    event.update({self.rules['query_key']: qk})
                self.check_for_match(qk, event, False)

    def get_match_str(self, match):
        lt = self.rules.get('use_local_time')
        fmt = self.rules.get('custom_pretty_ts_format')
        starttime = pretty_ts(dt_to_ts(ts_to_dt(lookup_es_key(match, self.ts_field)) - self.rules['timeframe']), lt, fmt)
        endtime = pretty_ts(lookup_es_key(match, self.ts_field), lt, fmt)
        if 'max_cardinality' in self.rules:
            message = ('A maximum of %d unique %s(s) occurred since last alert or between %s and %s\n\n' % (self.rules['max_cardinality'],
                                                                                                            self.rules['cardinality_field'],
                                                                                                            starttime, endtime))
        else:
            message = ('Less than %d unique %s(s) occurred since last alert or between %s and %s\n\n' % (self.rules['min_cardinality'],
                                                                                                         self.rules['cardinality_field'],
                                                                                                         starttime, endtime))
        return message


class BaseAggregationRule(RuleType):
    def __init__(self, *args):
        super(BaseAggregationRule, self).__init__(*args)
        bucket_interval = self.rules.get('bucket_interval')
        if bucket_interval:
            if 'seconds' in bucket_interval:
                self.rules['bucket_interval_period'] = str(bucket_interval['seconds']) + 's'
            elif 'minutes' in bucket_interval:
                self.rules['bucket_interval_period'] = str(bucket_interval['minutes']) + 'm'
            elif 'hours' in bucket_interval:
                self.rules['bucket_interval_period'] = str(bucket_interval['hours']) + 'h'
            elif 'days' in bucket_interval:
                self.rules['bucket_interval_period'] = str(bucket_interval['days']) + 'd'
            elif 'weeks' in bucket_interval:
                self.rules['bucket_interval_period'] = str(bucket_interval['weeks']) + 'w'
            else:
                raise EAException("Unsupported window size")

            if self.rules.get('use_run_every_query_size'):
                if total_seconds(self.rules['run_every']) % total_seconds(self.rules['bucket_interval_timedelta']) != 0:
                    raise EAException("run_every must be evenly divisible by bucket_interval if specified")
            else:
                if total_seconds(self.rules['buffer_time']) % total_seconds(self.rules['bucket_interval_timedelta']) != 0:
                    raise EAException("Buffer_time must be evenly divisible by bucket_interval if specified")

    def generate_aggregation_query(self):
        raise NotImplementedError()

    def add_aggregation_data(self, payload):
        for timestamp, payload_data in payload.items():
            if 'interval_aggs' in payload_data:
                self.unwrap_interval_buckets(timestamp, None, payload_data['interval_aggs']['buckets'])
            elif 'bucket_aggs' in payload_data:
                self.unwrap_term_buckets(timestamp, payload_data['bucket_aggs']['buckets'])
            else:
                self.check_matches(timestamp, None, payload_data)

    def unwrap_interval_buckets(self, timestamp, query_key, interval_buckets):
        for interval_data in interval_buckets:
            # Use bucket key here instead of start_time for more accurate match timestamp
            self.check_matches(ts_to_dt(interval_data['key_as_string']), query_key, interval_data)

    def unwrap_term_buckets(self, timestamp, term_buckets):
        for term_data in term_buckets:
            if 'interval_aggs' in term_data:
                self.unwrap_interval_buckets(timestamp, term_data['key'], term_data['interval_aggs']['buckets'])
            else:
                self.check_matches(timestamp, term_data['key'], term_data)

    def check_matches(self, timestamp, query_key, aggregation_data):
        raise NotImplementedError()

#Error Rate Rule Definition
class ErrorRateRule(BaseAggregationRule):
    """ A rule that determines error rate with sampling rate"""
    required_options = frozenset(['sampling', 'threshold','error_condition','unique_column'])
    def __init__(self, *args):
        super(ErrorRateRule, self).__init__(*args)

        self.ts_field = self.rules.get('timestamp_field', '@timestamp')
        self.rules['total_agg_key'] = self.rules['unique_column']
        self.rules['count_all_errors'] = True

        if ( 'error_calculation_method' in self.rules and self.rules['error_calculation_method']=='count_traces_with_errors' ):
            self.rules['count_all_errors'] = False

        # hardcoding uniq aggregation for total count
        self.rules['total_agg_type'] = "uniq"

    def calculate_err_rate(self,payload):
        for timestamp, payload_data in payload.items():
            if int(payload_data['total_count']) > 0:
                rate = float(payload_data['error_count'])/float(payload_data['total_count'])
                rate = float(rate)/float(self.rules['sampling'])
                rate = rate*100
                if 'threshold' in self.rules and rate > self.rules['threshold']:
                    match = {self.rules['timestamp_field']: timestamp, 'error_rate': rate, 'from': payload_data['start_time'], 'to': payload_data['end_time']}
                    self.add_match(match)

class MetricAggregationRule(BaseAggregationRule):
    """ A rule that matches when there is a low number of events given a timeframe. """
    required_options = frozenset(['metric_agg_key', 'metric_agg_type'])
    allowed_aggregations = frozenset(['min', 'max', 'avg', 'sum', 'cardinality', 'value_count'])
    allowed_percent_aggregations = frozenset(['percentiles'])

    def __init__(self, *args):
        super(MetricAggregationRule, self).__init__(*args)
        self.ts_field = self.rules.get('timestamp_field', '@timestamp')
        if 'max_threshold' not in self.rules and 'min_threshold' not in self.rules:
            raise EAException("MetricAggregationRule must have at least one of either max_threshold or min_threshold")

        self.metric_key = 'metric_' + self.rules['metric_agg_key'] + '_' + self.rules['metric_agg_type']

        if not self.rules['metric_agg_type'] in self.allowed_aggregations.union(self.allowed_percent_aggregations):
            raise EAException("metric_agg_type must be one of %s" % (str(self.allowed_aggregations)))
        if self.rules['metric_agg_type'] in self.allowed_percent_aggregations and self.rules['percentile_range'] is None:
            raise EAException("percentile_range must be specified for percentiles aggregation")

        self.rules['aggregation_query_element'] = self.generate_aggregation_query()

    def get_match_str(self, match):
        metric_format_string = self.rules.get('metric_format_string', None)
        message = 'Threshold violation, %s:%s %s (min: %s max : %s) \n\n' % (
            self.rules['metric_agg_type'],
            self.rules['metric_agg_key'],
            format_string(metric_format_string, match[self.metric_key]) if metric_format_string else match[self.metric_key],
            self.rules.get('min_threshold'),
            self.rules.get('max_threshold')
        )
        return message

    def generate_aggregation_query(self):
        if self.rules.get('metric_agg_script'):
            return {self.metric_key: {self.rules['metric_agg_type']: self.rules['metric_agg_script']}}
        query = {self.metric_key: {self.rules['metric_agg_type']: {'field': self.rules['metric_agg_key']}}}
        if self.rules['metric_agg_type'] in self.allowed_percent_aggregations:
            query[self.metric_key][self.rules['metric_agg_type']]['percents'] = [self.rules['percentile_range']]
            query[self.metric_key][self.rules['metric_agg_type']]['keyed'] = False
        return query

    def check_matches(self, timestamp, query_key, aggregation_data):
        if "compound_query_key" in self.rules:
            self.check_matches_recursive(timestamp, query_key, aggregation_data, self.rules['compound_query_key'], dict())

        else:
            if self.rules['metric_agg_type'] in self.allowed_percent_aggregations:
                #backwards compatibility with existing elasticsearch library
                #aggregation_data = {"doc_count":258757,"key":"appmailer","metric_qt_percentiles":{"values":[{"key":95,"value":0}]}}
                metric_val = aggregation_data[self.metric_key]['values'][0]['value']
            else:
                metric_val = aggregation_data[self.metric_key]['value']
            if self.crossed_thresholds(metric_val):
                match = {self.rules['timestamp_field']: timestamp,
                         self.metric_key: metric_val,
                         'metric_agg_value': metric_val
                         }
                metric_format_string = self.rules.get('metric_format_string', None)
                if metric_format_string is not None:
                    match[self.metric_key +'_formatted'] = format_string(metric_format_string, metric_val)
                    match['metric_agg_value_formatted'] = format_string(metric_format_string, metric_val)
                if query_key is not None:
                    match = expand_string_into_dict(match, self.rules['query_key'], query_key)
                self.add_match(match)

    def check_matches_recursive(self, timestamp, query_key, aggregation_data, compound_keys, match_data):
        if len(compound_keys) < 1:
            # shouldn't get to this point, but checking for safety
            return

        match_data[compound_keys[0]] = aggregation_data['key']
        if 'bucket_aggs' in aggregation_data:
            for result in aggregation_data['bucket_aggs']['buckets']:
                self.check_matches_recursive(timestamp,
                                             query_key,
                                             result,
                                             compound_keys[1:],
                                             match_data)
        else:
            if 'interval_aggs' in aggregation_data:
                metric_val_arr = [term[self.metric_key]['value'] for term in aggregation_data['interval_aggs']['buckets']]
            else:
                metric_val_arr = [aggregation_data[self.metric_key]['value']]
            for metric_val in metric_val_arr:
                if self.crossed_thresholds(metric_val):
                    match_data[self.rules['timestamp_field']] = timestamp
                    match_data[self.metric_key] = metric_val

                    # add compound key to payload to allow alerts to trigger for every unique occurence
                    compound_value = [match_data[key] for key in self.rules['compound_query_key']]
                    match_data[self.rules['query_key']] = ",".join([str(value) for value in compound_value])
                    self.add_match(match_data)

    def crossed_thresholds(self, metric_value):
        if metric_value is None:
            return False
        if 'max_threshold' in self.rules and float(metric_value) > self.rules['max_threshold']:
            return True
        if 'min_threshold' in self.rules and float(metric_value) < self.rules['min_threshold']:
            return True
        return False


class SpikeMetricAggregationRule(BaseAggregationRule, SpikeRule):
    """ A rule that matches when there is a spike in an aggregated event compared to its reference point """
    required_options = frozenset(['metric_agg_key', 'metric_agg_type', 'spike_height', 'spike_type'])
    allowed_aggregations = frozenset(['min', 'max', 'avg', 'sum', 'cardinality', 'value_count'])
    allowed_percent_aggregations = frozenset(['percentiles'])

    def __init__(self, *args):
        # We inherit everything from BaseAggregation and Spike, overwrite only what we need in functions below
        super(SpikeMetricAggregationRule, self).__init__(*args)

        # MetricAgg alert things
        self.metric_key = 'metric_' + self.rules['metric_agg_key'] + '_' + self.rules['metric_agg_type']

        if not self.rules['metric_agg_type'] in self.allowed_aggregations.union(self.allowed_percent_aggregations):
            raise EAException("metric_agg_type must be one of %s" % (str(self.allowed_aggregations)))
        if self.rules['metric_agg_type'] in self.allowed_percent_aggregations and self.rules['percentile_range'] is None:
            raise EAException("percentile_range must be specified for percentiles aggregation")

        # Disabling bucket intervals (doesn't make sense in context of spike to split up your time period)
        if self.rules.get('bucket_interval'):
            raise EAException("bucket intervals are not supported for spike aggregation alerts")

        self.rules['aggregation_query_element'] = self.generate_aggregation_query()

    def generate_aggregation_query(self):
        """Lifted from MetricAggregationRule"""
        if self.rules.get('metric_agg_script'):
            return {self.metric_key: {self.rules['metric_agg_type']: self.rules['metric_agg_script']}}
        query = {self.metric_key: {self.rules['metric_agg_type']: {'field': self.rules['metric_agg_key']}}}
        if self.rules['metric_agg_type'] in self.allowed_percent_aggregations:
            query[self.metric_key][self.rules['metric_agg_type']]['percents'] = [self.rules['percentile_range']]
        return query

    def add_aggregation_data(self, payload):
        """
        BaseAggregationRule.add_aggregation_data unpacks our results and runs checks directly against hardcoded cutoffs.
        We instead want to use all of our SpikeRule.handle_event inherited logic (current/reference) from
        the aggregation's "value" key to determine spikes from aggregations
        """
        for timestamp, payload_data in payload.items():
            if 'bucket_aggs' in payload_data:
                self.unwrap_term_buckets(timestamp, payload_data['bucket_aggs'])
            else:
                # no time / term split, just focus on the agg
                event = {self.ts_field: timestamp}
                if self.rules['metric_agg_type'] in self.allowed_percent_aggregations:
                    agg_value = list(payload_data[self.metric_key]['values'].values())[0]
                else:
                    agg_value = payload_data[self.metric_key]['value']
                self.handle_event(event, agg_value, 'all')
        return

    def unwrap_term_buckets(self, timestamp, term_buckets, qk=[]):
        """
        create separate spike event trackers for each term,
        handle compound query keys
        """
        for term_data in term_buckets['buckets']:
            qk.append(term_data['key'])

            # handle compound query keys (nested aggregations)
            if term_data.get('bucket_aggs'):
                self.unwrap_term_buckets(timestamp, term_data['bucket_aggs'], qk)
                # reset the query key to consider the proper depth for N > 2
                del qk[-1]
                continue

            qk_str = ','.join(qk)
            if self.rules['metric_agg_type'] in self.allowed_percent_aggregations:
                agg_value = list(term_data[self.metric_key]['values'].values())[0]
            else:
                agg_value = term_data[self.metric_key]['value']
            event = {self.ts_field: timestamp,
                     self.rules['query_key']: qk_str}
            # pass to SpikeRule's tracker
            self.handle_event(event, agg_value, qk_str)

            # handle unpack of lowest level
            del qk[-1]
        return

    def get_match_str(self, match):
        """
        Overwrite SpikeRule's message to relate to the aggregation type & field instead of count
        """
        message = 'An abnormal {0} of {1} ({2}) occurred around {3}.\n'.format(
            self.rules['metric_agg_type'], self.rules['metric_agg_key'], round(match['spike_count'], 2),
            pretty_ts(match[self.rules['timestamp_field']], self.rules.get('use_local_time'), self.rules.get('custom_pretty_ts_format'))
        )
        message += 'Preceding that time, there was a {0} of {1} of ({2}) within {3}\n\n'.format(
            self.rules['metric_agg_type'], self.rules['metric_agg_key'],
            round(match['reference_count'], 2), self.rules['timeframe'])
        return message


class PercentageMatchRule(BaseAggregationRule):
    required_options = frozenset(['match_bucket_filter'])

    def __init__(self, *args):
        super(PercentageMatchRule, self).__init__(*args)
        self.ts_field = self.rules.get('timestamp_field', '@timestamp')
        if 'max_percentage' not in self.rules and 'min_percentage' not in self.rules:
            raise EAException("PercentageMatchRule must have at least one of either min_percentage or max_percentage")

        self.min_denominator = self.rules.get('min_denominator', 0)
        self.match_bucket_filter = self.rules['match_bucket_filter']
        self.rules['aggregation_query_element'] = self.generate_aggregation_query()

    def get_match_str(self, match):
        percentage_format_string = self.rules.get('percentage_format_string', None)
        message = 'Percentage violation, value: %s (min: %s max : %s) of %s items\n\n' % (
            format_string(percentage_format_string, match['percentage']) if percentage_format_string else match['percentage'],
            self.rules.get('min_percentage'),
            self.rules.get('max_percentage'),
            match['denominator']
        )
        return message

    def generate_aggregation_query(self):
        return {
            'percentage_match_aggs': {
                'filters': {
                    'filters': {
                        'match_bucket': {
                            'bool': {
                                'must': self.match_bucket_filter
                            }
                        },
                        '_other_': {
                            'bool': {
                                'must_not': self.match_bucket_filter
                           }
                        }
                    }
                }
            }
        }

    def check_matches(self, timestamp, query_key, aggregation_data):
        match_bucket_count = aggregation_data['percentage_match_aggs']['buckets']['match_bucket']['doc_count']
        other_bucket_count = aggregation_data['percentage_match_aggs']['buckets']['_other_']['doc_count']

        if match_bucket_count is None or other_bucket_count is None:
            return
        else:
            total_count = other_bucket_count + match_bucket_count
            if total_count == 0 or total_count < self.min_denominator:
                return
            else:
                match_percentage = (match_bucket_count * 1.0) / (total_count * 1.0) * 100
                if self.percentage_violation(match_percentage):
                    match = {self.rules['timestamp_field']: timestamp, 'percentage': match_percentage, 'denominator': total_count}
                    percentage_format_string = self.rules.get('percentage_format_string', None)
                    if percentage_format_string is not None:
                        match['percentage_formatted'] = format_string(percentage_format_string, match_percentage)
                    if query_key is not None:
                        match = expand_string_into_dict(match, self.rules['query_key'], query_key)
                    self.add_match(match)

    def percentage_violation(self, match_percentage):
        if 'max_percentage' in self.rules and match_percentage > self.rules['max_percentage']:
            return True
        if 'min_percentage' in self.rules and match_percentage < self.rules['min_percentage']:
            return True
        return False
