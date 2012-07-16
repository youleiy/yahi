#!/usr/bin/env python
# -*- coding: utf-8 -*-
import sys
from os import path
import re
from datetime import datetime
import pygeoip
import httpagentparser
import fileinput
import csv
from json import load, loads, dump, dumps
from archery.barrack import mapping_row_iter
from archery.bow import Hankyu
from .field import log_pattern, date_pattern
from repoze.lru import CacheMaker
import argparse




####################### STATIC DATA ################################
HARDCODED_GEOIP_FILE = "data/GeoIP.dat"
### GLOBAL CACHE prettu multiprocessing unfirendly


####################" 


def build_filter_from_json(str_or_file, positive_logic):
    matcher = {}
    try:
        matcher.update(str_or_file)
    except TypeError:
            matcher = load(open(str_or_file))
    except ValueError:
        ## errno 2 <=> file not found
        matcher =  loads(str_or_file)


    if len(matcher):
        for field, regexp in matcher.items():
            matcher[field] = re.compile(regexp).match
        if positive_logic:
            return lambda data : all(
                matcher[k](data[k]) for k in matcher
            ) if data else False
        else:
            return lambda data : not(
                any(matcher[k](data[k]) for k in matcher )
            ) if data else False
 
   
####################### UTILITIES ################################
def dt_formater_from_format(date_format):
    def cdt_format(dt):
        return datetime.strptime(dt,date_format)
    return cdt_format


def normalize_user_agent(user_agent):
    """
    set default value for useragent and
    flatten httpuseragentparser result, in the form ua_%(key)s_%(subkey)s
    key in ["os", "browser", "dist" ]
    subkey in ["name", "version" ]
    """
    flat=dict()
    user_agent = {
        'os': {'name': "unknown", "version": 'unknown'},
        'browser': {'name': "unknown", "version": 'unknown'},
        'dist': {'name': "unknown", "version": 'unknown'},
        }
    user_agent.update( httpagentparser.detect(user_agent))
    flat=dict()
    for k in user_agent:
        for sub_key,v in user_agent[k].iteritems():
            flat.update({ "_".join(["ua" , k, sub_key]) :v}) 
    return flat
class Yahi:
    
    def shoot(
            self,
            group_by,
            ):
        """Produce a dict of the data found in the line.
        and use group_by to  group according to option (that can contain 
        a data_filter)
        * group_by : a lambda returning a Hankyu (dict)
            used to extract the valid informations
        * option.cache : cache strategy (beaker, repoze, dict, fixed, no_cache)
        * option.data_filter : f(data) => bool
            applies to the data dict, 
            if True extract the current data
        * option.diagnose array of string : 
          * match : tells on stderr wich line were rejected
          * rejected : tells stderr wich data were filtered out
        * option.skill : enable costly extraction of : 
            * geo_ip : geoip informations
            * user_agent : user_agent parsing
        * option.log_format : apache_log_combined or lighttpd
            tells the regexp to use to extract the fields from the line
            also used to select the datetime parser
        """
        self.clear_log()
        aggregator=Hankyu({})
        if 'user_agent' in self.skill:
            import httpagentparser
        
        look_for = log_pattern[self.log_format].search
        match = None
        dt_format = dt_formater_from_format(date_pattern[self.log_format])
        parse_user_agent = self.cache("user_agent")(normalize_user_agent)

        if "geo_ip" in self.skill:
            from pygeoip import GeoIP
            gi = GeoIP(self.geoip)
            country_by_ip = self.cache("geoip")(gi.country_code_by_addr)
        _input = fileinput.input(self.files)
        try:
            for line in _input:
                match = look_for(line)
                if match:
                    data = match.groupdict()
                    data['_datetime']=dt_format(data["datetime"])

                    if 'geo_ip' in self.skill:
                        data.update( {"country":country_by_ip(data["ip"])})
                    if 'user_agent' in self.skill:
                        data.update(
                            parse_user_agent(data["agent"])
                        )
                    if self.data_filter and not self.data_filter(data):
                        if "rejected" in self.diagnose:
                            if self.silent:
                                self.log["warning"]+=[ "REJECTED:at %s:%s:%s"%(_input.lineno(),_input.filename(),data) ]
                            else:
                                sys.stderr.write("at %s:%s:" % (
                                    _input.lineno(),_input.filename()) )
                                sys.stderr.write("REJECTED:{0}\n".format(data))
                    else:
                        aggregator += group_by(data)

                elif "match" in self.diagnose:
                    if self.silent:
                        self.log["warning"]+=["NOTMATCH:at %s:%s:\%s not match" % ( _input.lineno(),_input.filename(), line)]
                    else:
                        sys.stderr.write("at %s:%s:" % ( 
                            _input.lineno(),_input.filename()) )
                        sys.stderr.write("NOT MATCHED:{0}\n".format(line))
        except Exception as e:
            if self.silent:
                self.log["error"]+=["ARRG(%s):at %s:%s" % (e, _input.lineno(),_input.filename())]
            else:
                sys.stderr.write("ARRG:at %s:%s\n" % ( _input.lineno(),_input.filename()) )
                sys.stderr.write("CONTEXT:line %s:match %s:data : %s\n" % (line,match.groupdict(),data))
                raise Exception(e)
        
        finally:
            _input.close()
        return aggregator

    def clear_log(self):
        self.log=dict(error=[],warning=[]) 

    def __init__(self,*pos, **config):
        self.silent=False
        self.clear_log()
        self.config=config
        self.notch(*pos)
#################### CLI and DOC #################################

    def notch(self,*pos):
        """CLI parser
        returns an context object that contains all CLI params and values
        print context.help for more informations
        

        TODO replace CLI parsing with taking input from a dict"""

        parser = argparse.ArgumentParser(
            formatter_class=argparse.RawDescriptionHelpFormatter,
            description="""\
    Utility for parsing logs in the apache/nginx combined log format
    and output a json of various aggregatted metrics of frequentation :
         * by Geolocation (quite fuzzy but still);
         * by user agent;
         * by hour;
         * by day;
         * by browser;
         * by status code
         * of url by ip;
         * by ip;
         * by url;
         * and bandwidth by ip;

    Example :
    =========

    from stdin (useful for using zcat)
    **********************************
    zcat /var/log/apache.log.1.gz | parse_log.py  > dat1.json

    excluding IPs 192.168/16 and user agent containing Mozilla
    **********************************************************
    parse_log -o dat2.json -x '{ "ip" : "^192.168", "agent": "Mozill" }'  /var/log/apache*.log 

    Since archery is cool here is a tip for aggregating data
    >>> from archery.barrack import bowyer
    >>> from archery.bow import Hankyu
    >>> from json import load, dumps
    >>> dumps(
            bowyer(Hankyu,load(file("dat1.json"))) + 
            bowyer(Hankyu,load(file("dat2.json")))
        )

    Hence a usefull trick to merge your old stats with your new one
            """
             )
        parser.add_argument("-c",
            "--config",
            help="""specify a config file in json format for the command line arguments
            any command line arguments will disable values in the config""",
            metavar="FILE",
            default=None
            )
        parser.add_argument("-g",
            "--geoip",
            help="specify a path to a geoip.dat file",
            default=HARDCODED_GEOIP_FILE
        )
        parser.add_argument("-cs",
            "--cache-size",
            help="in conjonction with cp=fixed chooses dict size",
            default="10000"
        )
            
        
        parser.add_argument("-d",
            "--diagnose",
            help="""diagnose 
                list of space separated arguments :
                    **rejected** : will print on STDERR rejected parsed line,
                    **match** : will print on stderr data filtered out
            
            """,
            default="",
            nargs='+',
            
        )
            
        parser.add_argument("-in",
            "--include",
            help="""include from extracted data with
            a json (string or filename) in the form { "field" : "pattern" } """,
        )
        parser.add_argument(
            "--off",
            help="""turn off plugins : geo_ip to skip geoip, user_agent to
            turn httpagentparser off""",
            default=()
        )
        parser.add_argument("-x",
            "--exclude",
            help="""exclude from extracted data with
            a json (string or filename) in the form { "field" : "pattern" } """,
        )
        parser.add_argument("-f",
            "--output-format",
            help="""decide if output is in a specified formater amongst : csv, json,
            indented_json""",
            default="indented_json"
        )
        parser.add_argument("-lf",
            "--log-format",
            help="log format amongst apache_log_combined, lighttpd",
            default="apache_log_combined"
        )
        parser.add_argument("-lp",
            "--log-pattern",
            help="""add a custom named regexp for parsing log lines""",
        )
        parser.add_argument("-lpn","--log-pattern-name",
            help="""the name with witch you want to register the pattern""",
            default="custom"
        )

        parser.add_argument("-dp",
            "--date-pattern",
            help="""add a custom date format, usefull if and only if using 
            a custom log_pattern and date pattern differs from apache.""",
            default=date_pattern["apache_log_combined"]
        )


        parser.add_argument("-o",
            "--output-file",
            help="output file",
            type=argparse.FileType('w'),
            default=sys.stdout
        )
        parser.add_argument('files', nargs=argparse.REMAINDER)
        option=parser.parse_args(*pos)
        option.skill=[]

        # loading if needed options as a json
        if option.config or self.config:
            if option.config:
                config_args = load(open(option.config))
            else:
                config_args = self.config
            for k in config_args:
                setattr(option, k, config_args[k])
                if "output_file" == k:
                    option.output_file=open(config_args[k],"w")
        # sanity check
        if option.log_pattern and option.log_format != option.log_pattern_name:
            raise Exception("You want to register a new pattern and not use it?")

        # hu hu I wouldn't like to see a log pattern on the command line
        # advice: use the json to load load log format, or send a patch
        # it will be easier. 
        if option.log_pattern:
            log_pattern[option.log_pattern_name]=re.compile(option.log_pattern)
            if option.date_pattern:
                date_pattern[option.log_pattern_name]=option.date_pattern

        # crude filtering with regexps matching a regexp for given keys
        if not self.config.get('data_filter'):
            _data_filter = None
            _exclusive_filter = _inclusive_filter = None
            if option.include:
                _data_filter= _inclusive_filter = build_filter_from_json(
                    option.include,True
                )

            if option.exclude:
                _data_filter = _exclusive_filter = build_filter_from_json(
                    option.exclude,False
                )

            if _exclusive_filter and _inclusive_filter:
                _data_filter = lambda data : _exclusive_filter(data) and \
                    _inclusive_filter(data)

            option.data_filter=_data_filter
        option.output=dict( 
            csv = lambda aggreg : csv.writer(option.output_file).writerows( 
                mapping_row_iter(aggreg)
            ),
            json = lambda aggreg : option.output_file.write( dumps(aggreg)),
            indented_json = lambda aggreg : option.output_file.write(
                dumps(aggreg,indent=4
             )),
        )[option.output_format]

        if "geo_ip" not in option.off:
            option.skill+= [ "geo_ip" ]

        if "user_agent" not in option.off:
            option.skill+= [ "user_agent" ]
        
        option.cachemaker=CacheMaker()
        option.cache=lambda name : option.cachemaker.lrucache(
                name,
                option.cache_size,
            )
        option.help = parser.format_help()
        for k,v in option.__dict__.items():
            setattr(self,k,v)

       

