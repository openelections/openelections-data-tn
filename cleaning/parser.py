import xlrd
import requests
import csv

results = []

r = requests.get("https://sos-tn-gov-files.tnsosfiles.com/180802_ResultsbyPrecinct.xlsx")
workbook = xlrd.open_workbook(file_contents=r.content)
ws = workbook.sheets()[0]

for r in range(1, ws.nrows):
    row = ws.row_values(r)
    county = row[0]
    precinct = row[2]
    office = row[6]
    cand_votes = row[10:]
    cands_with_votes = zip(*[cand_votes[i::4] for i in range(4)])
    for cand, party, votes, fill in cands_with_votes:
        if votes != '':
#            if ' - ' in cand:
#                candidate, party = cand.split(' - ')
#                party = party.replace('(','').replace(')','')
#            else:
            candidate = cand
            results.append([county, precinct, office, None, party, candidate, votes])

with open("2018/20180802__tn__primary__precinct.csv", 'w') as csvfile:
    csvwriter = csv.writer(csvfile)
    csvwriter.writerow(['county','precinct','office','district','party','candidate','votes'])
    csvwriter.writerows(results)
