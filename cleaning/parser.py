import xlrd
import requests
import unicodecsv as csv

results = []

r = requests.get("http://sharetngov.tnsosfiles.com.s3.amazonaws.com/sos/election/results/2012-11/November2012.xlsx")
workbook = xlrd.open_workbook(file_contents=r.content)
ws = workbook.sheets()[0]

for r in range(1, ws.nrows):
    row = ws.row_values(r)
    county = row[0]
    precinct = row[2]
    office = row[7]
    district = row[5].strip()
    cand_votes = row[10:]
    cands_with_votes = zip(*[cand_votes[i::3] for i in range(3)])
    for idx, cand, votes in cands_with_votes:
        if int(votes) > 0 and row[9] != 'State General':
            if ' - ' in cand:
                candidate, party = cand.split(' - ')
                party = party.replace('(','').replace(')','')
            else:
                party = row[9].replace(" Primary","").strip()
                candidate = cand
            results.append([county, precinct, office, district, party, candidate.replace('. ','', 1), votes])

with open("2012/20121106__tn__general__precinct.csv", 'wb') as csvfile:
    csvwriter = csv.writer(csvfile, encoding='utf-8')
    csvwriter.writerow(['county','precinct','office','district','party','candidate','votes'])
    csvwriter.writerows(results)
