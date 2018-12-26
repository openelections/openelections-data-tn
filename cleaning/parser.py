import xlrd
import requests
import csv

results = []

r = requests.get("https://sos-tn-gov-files.tnsosfiles.com/Nov%202018%20General%20results.xlsx")
workbook = xlrd.open_workbook(file_contents=r.content)
ws = workbook.sheets()[0]

for r in range(1, ws.nrows):
    row = ws.row_values(r)
    county = row[0]
    precinct = row[2]
    if 'Tennessee House of Representatives' in row[7]:
        office = 'State Representative'
        district = row[7].split(' District ')[1]
    elif 'Tennessee Senate' in row[7]:
        office = 'State Senate'
        district = row[7].split(' District ')[1]
    elif 'United States House of Representatives' in row[7]:
        office = 'U.S. House'
        district = row[7].split(' District ')[1]
    else:
        office = row[7]
        district = None
    cand_votes = row[11:]
    cands_with_votes = zip(*[cand_votes[i::4] for i in range(4)])
    for cand, party, votes, fill in cands_with_votes:
        if votes != '':
#            if ' - ' in cand:
#                candidate, party = cand.split(' - ')
#                party = party.replace('(','').replace(')','')
#            else:
            candidate = cand
            results.append([county, precinct, office, district, party, candidate, votes])

with open("2018/20181106__tn__general__precinct.csv", 'w') as csvfile:
    csvwriter = csv.writer(csvfile, quoting=csv.QUOTE_NONNUMERIC)
    csvwriter.writerow(['county','precinct','office','district','party','candidate','votes'])
    csvwriter.writerows(results)
