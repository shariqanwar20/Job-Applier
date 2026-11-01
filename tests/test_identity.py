import pytest
from jobapplier.identity import posting_identity

@pytest.mark.parametrize("url,expected", [
    ("https://job-boards.greenhouse.io/affirm/jobs/8042399003?utm_source=freehire.me", "greenhouse:affirm:8042399003"),
    ("https://boards.greenhouse.io/affirm/jobs/8042399003?gh_src=indeed", "greenhouse:affirm:8042399003"),
    ("https://boards.greenhouse.io/embed/job_app?for=affirm&token=8042399003", "greenhouse:affirm:8042399003"),
    ("https://jobs.lever.co/acme/1b2c3d4e-0000-1111-2222-333344445555/apply", "lever:acme:1b2c3d4e-0000-1111-2222-333344445555"),
    ("https://jobs.ashbyhq.com/pebl/03cd41b2-5acd-4f6c-be73-5c6126170af0?utm_source=freehire.me", "ashby:pebl:03cd41b2-5acd-4f6c-be73-5c6126170af0"),
    ("https://td.wd3.myworkdayjobs.com/TD_Bank_Careers/job/Toronto-Ontario/Software-Engineer-II_R_1516370?utm_source=freehire.me", "workday:td:software-engineer-ii_r_1516370"),
    ("https://td.wd3.myworkdayjobs.com/en-US/TD_Bank_Careers/job/Mississauga/Software-Engineer-II_R_1516370/apply", "workday:td:software-engineer-ii_r_1516370"),
    ("https://jobs.apple.com/en-us/details/200692697/software-engineer-xcode-cloud/?utm_source=freehire.me", "url:jobs.apple.com/en-us/details/200692697/software-engineer-xcode-cloud"),
    ("https://en-ca.whatjobs.com/pub_api__cpl__156344138__7103?geoID=6399", None),
    ("https://www.linkedin.com/jobs/view/4485119071", None),
    ("", None),
])
def test_posting_identity(url, expected):
    assert posting_identity(url) == expected
