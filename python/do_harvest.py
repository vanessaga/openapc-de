#!/usr/bin/env python3
# -*- coding: UTF-8 -*-

import argparse
import datetime
import os
import re
import xml.etree.ElementTree as ET

from collections import OrderedDict
from csv import DictReader
from urllib.error import HTTPError, URLError
from urllib.request import urlopen, Request

import openapc_toolkit as oat
import opencost_toolkit_v2 as octk

ARG_HELP_STRINGS = {
    "integrate": ('Integrate changes in harvested data into existing ' +
                  'collected files ("all_harvested_articles.csv" and '+
                  '"all_harvested_articles_enriched.csv")'),
    "output": 'Write raw harvested data to disk',
    "links": "Print OAI GetRecord links for all harvested articles, useful " +
             "for inspecting and debugging the original data",
    "validate": "Do not process any data and validate all records against the " +
                "openCost XSD schema instead. Only works for sources with type " +
                "'opencost'",
    "force_update": "Download a fresh copy of the openCost XSD from GitHub. Only "+
                    "works in connection with the -v option"
}

def integrate_changes(articles, file_path, enriched_file=False, dry_run=False):
    '''
    Update existing entries in a previously created harvest file.
    
    Args:
        articles: A list of article dicts, as retured by oai_harvest()
        file_path: Path to the CSV file the new values should be integrated into.
        enriched_file: If true, columns which are overwritten during enrichment
                       will not be updated
        dry_run: Do not make any changes to the file (but still report changes and
                 return the list of unencountered articles)
    Returns:
        A reduced list of article dicts, containing those which did not
        find a matching DOI in the file (Order preserved).
    '''

    messages = {
        'wet': {
            'start': 'Integrating changes in harvest data into existing file {}',
            'line_change': 'Line {} ({}): Updating value in column {} ("{}" -> "{}")',
            'remove': 'PID {} no longer found in harvest data, removing article',
        },
        'dry': {
            'start': 'Dry Run: Comparing harvest data to existing file {}',
            'line_change': 'Line {} ({}): Change in column {} ("{}" -> "{}")',
            'remove': 'PID {} no longer found in harvest data, article would be removed',
        }
    }

    messages = messages['dry'] if dry_run else messages['wet']

    if not os.path.isfile(file_path):
        return articles
    enriched_blacklist = ["institution", "publisher", "journal_full_title", "issn", "license_ref", "pmid"]
    article_dict = OrderedDict()
    for article in articles:
        # Harvested articles use OAI record IDs in the url field as PID.
        url = article["url"]
        if oat.has_value(url):
            article_dict[url] = article
    updated_lines = []
    fieldnames = None
    unmatched_keys = []
    with open(file_path, "r") as f:
        reader = DictReader(f)
        fieldnames = reader.fieldnames
        updated_lines.append(list(fieldnames)) #header
        oat.print_y(messages["start"].format(file_path))
        for line in reader:
            url = line["url"]
            if not oat.has_value(line["institution"]):
                # Do not change empty lines
                updated_lines.append([line[key] for key in fieldnames])
                continue
            line_num = reader.reader.line_num
            if url in article_dict:
                for key, value in article_dict[url].items():
                    if key not in line:
                        if key not in unmatched_keys:
                            unmatched_keys.append(key)
                        continue
                    if enriched_file and key in enriched_blacklist:
                        continue
                    if key == "euro":
                        old_euro = oat._auto_atof(line[key])
                        new_euro = oat._auto_atof(value)
                        if old_euro is not None and new_euro is not None:
                            if old_euro == new_euro:
                                continue
                    if key in line and value != line[key]:
                        oat.print_g(messages["line_change"].format(line_num, line["url"], key, line[key], value))
                        line[key] = value
                del(article_dict[url])
                updated_line = [line[key] for key in fieldnames]
                updated_lines.append(updated_line)
            else:
                oat.print_r(messages["remove"].format(url))
    if unmatched_keys:
        msg = ("WARNING: There were unmatched keys in the harvested " +
               "data which do not exist in the all_harvested_articles " +
               "file: {}\nThis might occur if the repo switched to " +
               "another data format (e.g. from intact to opencost), in " +
               "this case you should delete the old all_harvested files " +
               "and start new ones.")
        oat.print_y(msg.format(unmatched_keys))
    if not dry_run:
        with open(file_path, "w") as f:
            mask = oat.OPENAPC_STANDARD_QUOTEMASK if enriched_file else None
            writer = oat.OpenAPCUnicodeWriter(f, quotemask=mask, openapc_quote_rules=True, has_header=True)
            writer.write_rows(updated_lines)
    return list(article_dict.values())

def oai_harvest(basic_url, metadata_prefix=None, oai_set=None, processing=None, out_file_suffix=None, data_type="intact", validate_only=False, force_update=False, record_url=None):
    """
    Harvest records via OAI-PMH
    """
    namespaces = {
        "opencost": "https://opencost.de",
        "oai_2_0": "http://www.openarchives.org/OAI/2.0/",
        "intact": "http://intact-project.org"
    }
    processing_regex = re.compile(r"'(?P<target>\w*?)':'(?P<generator>.*?)'")
    variable_regex = re.compile(r"%(\w*?)%")
    token_xpath = ".//oai_2_0:resumptionToken"
    url = basic_url + "?verb=ListRecords"
    if metadata_prefix:
        url += "&metadataPrefix=" + metadata_prefix
    if oai_set:
        url += "&set=" + oai_set
    processing_instructions = None
    if processing:
        match = processing_regex.match(processing)
        if match:
            groupdict = match.groupdict()
            target = groupdict["target"]
            generator = groupdict["generator"]
            variables = variable_regex.search(generator).groups()
            processing_instructions = {
                "generator": generator,
                "variables": variables,
                "target": target
            }
        else:
            print_r("Error: Unable to parse processing instruction!")
    record_url = basic_url + "?verb=GetRecord"
    if metadata_prefix:
        record_url += "&metadataPrefix=" + metadata_prefix
    oat.print_b("Harvesting from " + url)
    file_output = ""
    xml_content_strings = []
    while url is not None:
        try:
            request = Request(url)
            url = None
            response = urlopen(request)
            content_string = response.read()
            xml_content_strings.append(content_string)
            root = ET.fromstring(content_string)
            if out_file_suffix:
                file_output += content_string.decode()
            token = root.find(token_xpath, namespaces)
            if token is not None and token.text is not None:
                url = basic_url + "?verb=ListRecords&resumptionToken=" + token.text
        except HTTPError as httpe:
            code = str(httpe.getcode())
            print("HTTPError: {} - {}".format(code, httpe.reason))
        except URLError as urle:
            print("URLError: {}".format(urle.reason))
    if out_file_suffix:
        with open("raw_harvest_data_" + out_file_suffix, "w") as out:
            out.write(file_output)
    if data_type == "intact":
        return oat.process_intact_xml(processing_instructions, *xml_content_strings)
    elif data_type == "opencost":
        return octk.process_opencost_oai_records(processing_instructions, validate_only, force_update, record_url, *xml_content_strings)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-i", "--integrate", help=ARG_HELP_STRINGS["integrate"], action="store_true")
    parser.add_argument("-o", "--output", help=ARG_HELP_STRINGS["output"], action="store_true")
    parser.add_argument("-l", "--print_record_links", help=ARG_HELP_STRINGS["links"], action="store_true")
    parser.add_argument("-v", "--validate_only", help=ARG_HELP_STRINGS["validate"], action="store_true")
    parser.add_argument("-u", "--force_update", help=ARG_HELP_STRINGS["force_update"], action="store_true")
    args = parser.parse_args()

    with open("harvest_list.csv", "r") as harvest_list:
        reader = DictReader(harvest_list)
        for line in reader:
            basic_url = line["basic_url"]
            if line["active"] == "TRUE":
                oai_set = line["oai_set"] if len(line["oai_set"]) > 0 else None
                prefix = line["metadata_prefix"] if len(line["metadata_prefix"]) > 0 else None
                processing = line["processing"] if len(line["processing"]) > 0 else None
                repo_type = line["type"]
                directory = os.path.join("..", line["directory"])
                out_file_suffix = os.path.basename(line["directory"]) if args.output else None
                if args.validate_only:
                    if repo_type != "opencost":
                        oat.print_y("Skipping source " + basic_url + " - Validation is not possible since it is not an openCost repository")
                        continue
                    oat.print_g("Starting validation run for source " + basic_url)
                    oai_harvest(basic_url, prefix, oai_set, processing, out_file_suffix, repo_type, args.validate_only, args.force_update)
                    continue
                oat.print_g("Starting harvest for source " + basic_url)
                articles = oai_harvest(basic_url, prefix, oai_set, processing, out_file_suffix, repo_type, args.validate_only, args.force_update)
                harvest_file_path = os.path.join(directory, "all_harvested_articles.csv")
                enriched_file_path = os.path.join(directory, "all_harvested_articles_enriched.csv")
                new_article_dicts = integrate_changes(articles, harvest_file_path, False, not args.integrate)
                integrate_changes(articles, enriched_file_path, True, not args.integrate)
                if repo_type == 'intact':
                    header = list(oat.OAI_COLLECTION_CONTENT.keys())
                elif repo_type == 'opencost':
                    header = list(octk.OPENCOST_EXTRACTION_FIELDS.keys())
                new_articles = [header]
                for article_dict in new_article_dicts:
                    new_articles.append([article_dict[key] for key in header])
                now = datetime.datetime.now()
                date_string = now.strftime("%Y_%m_%d")
                file_name = "new_articles_" + date_string + ".csv"
                target = os.path.join(directory, file_name)
                with open(target, "w") as t:
                    writer = oat.OpenAPCUnicodeWriter(t, openapc_quote_rules=True, has_header=True)
                    writer.write_rows(new_articles)
            else:
                oat.print_y("Skipping inactive source " + basic_url)

if __name__ == '__main__':
    main()
