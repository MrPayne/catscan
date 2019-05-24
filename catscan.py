"""Take list of hosts/URIs or Nmap XML as input, scan hosts on given ports,
and return a searchable and sortable HTML report."""

import argparse
from argparse import RawDescriptionHelpFormatter
from datetime import datetime
import hashlib
from functools import partial
from multiprocessing.dummy import Pool as ThreadPool
import re

import jinja2
from lxml import html, etree
import requests
import urllib3
import xmltodict


results = {}
datatables_css = '<link rel="stylesheet" type="text/css" href="./css/jquery.dataTables.min.css">'
jquery = '<script type="text/javascript" charset="utf8" src="./js/jquery-3.3.1.min.js"></script>'
datatables = '<script type="text/javascript" charset="utf8" src="./js/jquery.dataTables.min.js"></script>'
datatables_celledit = '<script type="text/javascript" charset="utf8" src="./js/dataTables.cellEdit.js"></script>'

jinja_env = jinja2.Environment(trim_blocks=True)
TEMPLATE_HTML_REPORT = jinja_env.from_string("""\
<html>
  <head>
    <title>Catscan Report for {{ start_time }}</title>
    {{ datatables_css }}
    {% raw %}
    <style type="text/css" class="init">
        body {font-family:Arial;}
    </style>
    {% endraw %}
    {{ jquery }}
    {{ datatables }}
    {% if notes_column %}
    {{ datatables_celledit }}
    {% endif %}
    {% raw %}
    <script type="text/javascript" class="init">
      $(document).ready( function () {
        var hosts_table = $('#all_hosts').DataTable( {
          "pageLength": 10
        });
    {% endraw %}
        {% if notes_column %}
        {% raw %}
        function myCallbackFunction (updatedCell, updatedRow, oldValue) {
        }
        hosts_table.MakeCellsEditable({
          "onUpdate": myCallbackFunction,
          "columns": [5]
        });
        {% endraw %}
        {% endif %}
        {% raw %}
        var titles_table = $('#unique_titles').DataTable( {
          "initComplete": function () {
            var api = this.api();
            api.$('td').click( function () {
              $(all_hosts).DataTable().search( this.innerHTML ).draw();
              });                      
            }
        });
        {% endraw %}
        {% if notes_column %}
        {% raw %}
        titles_table.MakeCellsEditable({
          "onUpdate": myCallbackFunction,
          "columns": [2]
        });
        {% endraw %}
        {% endif %}
        {% raw %}
        var content_table = $('#unique_content').DataTable( {
          "initComplete": function () {
            var api = this.api();
            api.$('td').click( function () {
              $(all_hosts).DataTable().search( this.innerHTML ).draw();
              });                      
            }
        });
        {% endraw %}
        {% if notes_column %}
        {% raw %}
        content_table.MakeCellsEditable({
          "onUpdate": myCallbackFunction,
          "columns": [3]
        });
        {% endraw %}
        {% endif %}
      {% raw %}
      });
      </script>
      <script>
      //Adapted from https://www.codexworld.com/export-html-table-data-to-csv-using-javascript/
      function downloadCSV(csv, filename) {
      var csvFile;
      var downloadLink;
      csvFile = new Blob([csv], {type: "text/csv"});
      downloadLink = document.createElement("a");
      downloadLink.download = filename;
      downloadLink.href = window.URL.createObjectURL(csvFile);
      downloadLink.style.display = "none";
      document.body.appendChild(downloadLink);
      downloadLink.click();
      }
      function exportToCSV(table, filename) {
        var csv = [];
        var rows = document.getElementById(table).rows;
        for (var i = 0; i < rows.length; i++) {
          var row = [], cols = rows[i].cells;
          for (var j = 0; j < cols.length; j++) 
            row.push(cols[j].innerText);
            csv.push(row.join(","));        
        }
     downloadCSV(csv.join("\\n"), filename);
     }
     function clearSearch(target) {
       $(target).DataTable().search( "" ).draw();
     }
     </script>
    {% endraw %}  
  </head>
  <body>
    <h1 align="center">All Hosts</h1>
    <button onclick="clearSearch(all_hosts)" style="float: right;">Clear Search</button><br><br>
    <table id="all_hosts" class="display">
      <thead>
        <tr>
          <th>URI</th>
          <th>Page title</th>
          <th>Response Code</th>
          <th>MD5 Hash</th>
          {% if redirect_column %}
          <th>Redirect</th>
          {% endif %}
          {% if notes_column %}
          <th>Notes</th>
          {% endif %}
        </tr>
      </thead>
      <tbody>
        {% for key, value in results.items() %}
        <tr><td><a href={{ key }} target="_blank"</a>{{ key }}</td><td>{{ value[0] }}</td><td>{{ value[1] }}</td><td>{{ value[2] }}</td>{% if redirect_column %}<td>{{ value[3] }}</td>{% endif %}{% if notes_column %}<td>{{ "" }}</td>{% endif %}</tr>
        {% endfor %}
      </tbody>
    </table>
    <button onclick="exportToCSV('all_hosts', 'all_hosts.csv')">Save as CSV File</button>
    <br><br>
    <h1 align="center">Hosts by Title</h1>
    <button onclick="clearSearch(unique_titles)" style="float: right;">Clear Search</button><br><br>
    <table id="unique_titles" class="display">
      <thead>
        <tr>
          <th>Page Title</th>
          <th>Count</th>
          {% if notes_column %}
          <th>Notes</th>
          {% endif %}
        </tr>
      </thead>
      <tbody>
        {% for key, value in title_counts.items() %}
        <tr><td>{{ key }}</td><td>{{ value }}</td>{% if notes_column %}<td>{{ "" }}</td>{% endif %}</tr>
        {% endfor %}
      </tbody>
    </table>
    <button onclick="exportToCSV('unique_titles', 'unique_titles.csv')">Save as CSV File</button>
    <br><br>
    <h1 align="center">Hosts by Content</h1>
    <button onclick="clearSearch(unique_content)" style="float: right;">Clear Search</button><br><br>
    <table id="unique_content" class="display">
      <thead>
        <tr>
        <th>MD5 Hash</th>
        <th>Title</th>
        <th>Count</th>
        {% if notes_column %}
        <th>Notes</th>
        {% endif %}
        </tr>
      </thead>
      <tbody>
        {% for key, value in content_counts.items() %}
        <tr><td>{{ key }}</td><td>{{ value[1] }}</td><td>{{ value[0] }}</td>{% if notes_column %}<td>{{ "" }}</td>{% endif %}</tr>
        {% endfor %}
      </tbody>
    </table>
    <button onclick="exportToCSV('unique_content', 'unique_content.csv')">Save as CSV File</button>
    <br><br>
  </body>
</html>
""")


def parse_nmap_xml(nmap_file, ports):
    """Parse an Nmap .xml file for open HTTP(S) ports. Return a list."""
    hosts = []
    nmap_scan = xmltodict.parse(nmap_file.read())
    for host in nmap_scan['nmaprun']['host']:
        ipv4_addr = host['address']['@addr']
        if isinstance(host['ports']['port'], list):
            for port in host['ports']['port']:
                if int(port['@portid']) in ports:
                    hosts.append(f"{ipv4_addr}:{port['@portid']}")
        else:
            if int(host['ports']['port']['@portid']) in ports:
                hosts.append(f"{ipv4_addr}:{host['ports']['port']['@portid']}")
    scan_set = {'https://' + host if host[-3:] == '443' else 'http://' + host for host in hosts}
    return scan_set, len(scan_set)


def build_list(list_file, ports):
    """Read a text file of IPs or URLs, prepend protocol and append port as necessary.
    Return a set for scanning"""
    regex = re.compile(r"^(https?:\/\/)?.+?(:[0-9]{0,5})?$")
    scan_set = set()
    lines = list_file.readlines()
    for line in lines:
        line = re.match(regex, line)
        if line[1] and line[2]: #protocol and port
            scan_set.add(line[0])
        if line[1] and not line[2]: #protocol no port
            if line[1] == 'https://':
                scan_set.add(line[0])
            else:
                for port in ports:
                    if str(port) != '443': #If the list includes a URL with just HTTP, it will not automatically get an HTTPS variant added.
                        uri = line[0] + ':' + str(port)
                        scan_set.add(uri)
        if not line[1] and line[2]: #no protocol but port
            if line[2] == ':443':
                uri = 'https://' + line[0]
            else:
                uri = 'http://' + line[0]
            scan_set.add(uri)
        if not line[1] and not line[2]: #neither protocol nor port
            for port in ports:
                if str(port) == '443':
                    uri = 'https://' + line[0] + ':' + str(port)
                else:
                    uri = 'http://' + line[0] + ':' + str(port)
                scan_set.add(uri)
    return scan_set, len(scan_set)


def scan(timeout, validate_certs, no_redirect, user_agent, verbose, uri):
    """Make requests to the provided URIs and save attributes of the responses."""
    headers = {'User-Agent': user_agent}
    parser = etree.HTMLParser()  # Build parser so multiple threads don't use a global parser
    try:
        resp = requests.get(uri, headers=headers, timeout=timeout, verify=validate_certs,
                            allow_redirects=no_redirect)  # if response empty, don't add
        content = resp.content
        if resp.content:
            resp_hash = hashlib.md5(resp.content).hexdigest()
            tree = html.fromstring(content, parser=parser)
            try:
                title = tree.find('.//title').text
            except AttributeError:
                title = '<none>'
                if verbose:
                    print(f'[-] {uri} - attribute error; page may lack title element')
            if no_redirect is False:
                results[uri] = [title, resp.status_code, resp_hash]
            elif no_redirect is True:
                if resp.url.strip('/') != uri:
                    results[uri] = [title, resp.status_code, resp_hash, f'<a href="{resp.url}"</a>{resp.url}']
                elif resp.url.strip('/') == uri:
                    results[uri] = [title, resp.status_code, resp_hash, 'No redirect.']
        else:
            results[uri] = ['<none>', resp.status_code, '<no page content>', '<no page content>']
    except requests.exceptions.ConnectTimeout:
        if verbose:
            print(f'[-] {uri} - connection timeout')
        results[uri] = ['Connection timeout.'] * 4
    except requests.exceptions.ReadTimeout:
        if verbose:
            print(f'[-] {uri} - read timeout')
        results[uri] = ['Read timeout.'] * 4
    except requests.exceptions.SSLError:
        if verbose:
            print(f'[-] {uri} - certificate verification failed')
        results[uri] = ['Certificate error.'] * 4
    # Since requests uses other libraries (socket, httplib, urllib3) a requests ConnectionError can actually be any number of errors raised by those libraries.
    except requests.exceptions.ConnectionError as e:
        if 'BadStatusLine' in str(e.args[0]):
            if verbose:
                print(f'[-] {uri} - malformed page (consider visiting manually)')
            results[uri] = ['Malformed page.'] * 4
        elif 'Connection refused' in str(e.args[0]):
            if verbose:
                print(f'[-] {uri} - connection refused')
            results[uri] = ['Connection refused.'] * 4
        elif 'Connection reset by peer' in str(e.args[0]):
            if verbose:
                print(f'[-] {uri} - connection reset by peer')
            results[uri] = ['Connection reset by peer.'] * 4
        else:  # Catch all the others
            if verbose:
                print(f'[-] {uri} - unhandled exception: {str(e)}')
            results[uri] = ['Unhandled exception.'] * 4


def main():
    """Scan a list of hosts and generate an HTML report. Return nothing."""
    user_agent = '''Mozilla/5.0 (Windows NT 6.1; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/40.0.2214.85 Safari/537.36'''

    parser = argparse.ArgumentParser(description='Scan and categorize web servers using a searchable / sortable HTML report.',
                                     epilog='list input: python3 catscan.py -l <ips.txt> -p 80 443 8080 -t 10\n'
                                            'Nmap xml:   python3 catscan.py -x <nmap.xml>',
                                     formatter_class=RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('-x', '--xml', dest='nmap_xml', type=argparse.FileType('r'),
                       help='Use an Nmap XML file as input.')
    group.add_argument('-l', '--list', dest='host_list', type=argparse.FileType('r'),
                       help='Text file containing list of IPs or hostnames separated by line.')
    parser.add_argument('-p', '--ports', dest='ports', nargs='+', required=False, default=[80, 443],
                        help='List of ports to scan. Default 80 and 443.')
    parser.add_argument('-t', '--threads', dest='threads', type=int, default=10,
                        help='Number of threads. Default 10.')
    parser.add_argument('-T', '--timeout', dest='timeout', type=int, default=5,
                        help='Timeout in seconds. Default 5.')
    parser.add_argument('-o', '--output', dest='output', required=False,
                        help='Name of HTML report. Default based on date/time.')
    parser.add_argument('-u', '--user-agent', dest='user_agent', required=False, default=user_agent,
                        help='User-Agent string.')
    parser.add_argument('-r', '--no-redirect', dest='no_redirect', required=False, default=True, action='store_false',
                        help='Do not follow redirects. Catscan follows redirects by default.')
    parser.add_argument('-k', '--validate', dest='validate_certs', required=False, default=False,
                        action='store_true', help='Validate certificates. Default false.')
    parser.add_argument('-n', '--notes', dest='notes', required=False, default=False,
                        action='store_true')
    parser.add_argument('-v', '--verbose', dest='verbose', required=False, default=False,
                        action='store_true')
    args = parser.parse_args()

    nmap_file = args.nmap_xml
    list_file = args.host_list
    ports = args.ports
    timeout = args.timeout
    threads = args.threads
    output = args.output
    user_agent = args.user_agent
    no_redirect = args.no_redirect
    validate_certs = args.validate_certs
    notes = args.notes
    verbose = args.verbose
    start_time = datetime.now()
    if not output:
        output = f'catscan_report_{start_time.strftime("%a_%d%b%Y_%H%M").lower()}.html'
    if nmap_file:
        scan_list, host_count = parse_nmap_xml(nmap_file, ports)
    elif list_file:
        scan_list, host_count = build_list(list_file, ports)
    func = partial(scan, timeout, validate_certs, no_redirect, user_agent, verbose)
    pool = ThreadPool(threads)
    pool.map(func, scan_list)
    pool.close()
    pool.join()
    end_scan_time = datetime.now()
    if verbose:
        print(f'[*] Scan time: {(end_scan_time - start_time).total_seconds()} seconds')
    print(f'[*] {str(host_count)} hosts scanned')
    end_run_time = datetime.now()
    print(f'[*] Total run time: {(end_run_time - start_time).total_seconds()} seconds')

    title_counts, content_counts = {}, {}
    for value in results.values():
        if value[0] in title_counts:
            title_counts[value[0]] += 1
        else:
            title_counts[value[0]] = 1
        if value[2] in content_counts:
            content_counts[value[2]][0] += 1
        else:
            content_counts[value[2]] = [1, value[0]]

    with open(output, 'w') as html_report:
        html_report.write(TEMPLATE_HTML_REPORT.render(
            start_time=start_time.strftime("%a_%d%b%Y_%H%M").lower(),
            datatables_css=datatables_css,
            jquery=jquery,
            datatables=datatables,
            datatables_celledit=datatables_celledit,
            redirect_column=no_redirect,
            notes_column=notes,
            results=results,
            title_counts=title_counts,
            content_counts=content_counts
        ))

    print(f'[*] Report written to {output}')

if __name__ == '__main__':
    # Disable "InsecureRequestWarning: Unverified HTTPS request is being made.
    # Adding certificate verification is strongly advised." warning.
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    main()
