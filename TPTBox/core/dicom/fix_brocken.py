import pickle
from pathlib import Path

from tqdm import tqdm

from TPTBox import BIDS_FILE, NII, BIDS_Global_info
from TPTBox.core.dicom.dicom_extract import extract_folder

# source_folder = Path("/DATA/NAS/datasets_source/epi/NAKO/NAKO-732_Nachlieferung_20_25/")
source_folder = Path("/DATA/NAS/datasets_source/epi/NAKO/NAKO_2D_issue/")
source_folders = {
    "mevibe": {
        "rule": "part",
        "eco1-pip1": "ME_vibe_fatquant_pre_Eco1_PIP1",
        "eco4-pop1": "ME_vibe_fatquant_pre_Eco4_POP1",
        "fat-fraction": "ME_vibe_fatquant_pre_Output_FP",
        "water-fraction": "ME_vibe_fatquant_pre_Output_WP",
        "eco3-in1": "ME_vibe_fatquant_pre_Eco3_IN1",
        "fat": "ME_vibe_fatquant_pre_Output_F",
        "water": "ME_vibe_fatquant_pre_Output_W",
        "eco2-opp2": "ME_vibe_fatquant_pre_Eco2_OPP2",
        "eco5-arb1": "ME_vibe_fatquant_pre_Eco5_ARB1",
        "r2s": "ME_vibe_fatquant_pre_Output_R2s_Eff",
        "eco0-opp1": "ME_vibe_fatquant_pre_Eco0_OPP1",
    }
}
# [
#    "III_T2_TSE_SAG_LWS",
#    "II_T2_TSE_SAG_BWS",
#    "I_T2_TSE_SAG_HWS",
#    "T2_HASTE_TRA_COMPOSED",
#    "PD_FS_SPC_COR",
#    "3D_GRE_TRA_F",
#    "3D_GRE_TRA_in",
#    "3D_GRE_TRA_opp",
#    "3D_GRE_TRA_W",
# ]


def test_nii(path: Path | str | BIDS_FILE):
    if isinstance(path, str):
        path = Path(path)
    if path.exists():
        try:
            NII.load(path, True).copy().unique()
        except Exception:
            return False
    return True


def find_all_broken(path: str = "/DATA/NAS/datasets_processed/NAKO/dataset-nako/", parents=None):
    brocken = []
    subj_id = 0
    with open("broken.pkl", "rb") as w:
        brocken = pickle.load(w)
        subj_id = int(brocken[-1].get("sub"))
        print(f"{len(brocken)=}; continue at {subj_id=}")

    if parents is None:
        parents = ["rawdata"]
    bgi = BIDS_Global_info([path], parents=parents)
    for s, subj in tqdm(bgi.enumerate_subjects(sort=True)):
        if int(s) <= subj_id:
            continue
        q = subj.new_query(flatten=True)
        q.filter_filetype("nii.gz")
        for f in q.loop_list():
            if not test_nii(f):
                print("BROKEN:", f)
                brocken.append(f)
                with open("broken.pkl", "wb") as w:
                    pickle.dump(brocken, w)
                with open("broken2.pkl", "wb") as w:
                    pickle.dump(brocken, w)


source_folder_encrypted = Path("/DATA/NAS/datasets_source/epi/NAKO/NAKO-732_Nachlieferung_20_25/")


def test_and_replace(out_folder="/media/data/NAKO/dataset-nako2"):
    with open("/run/user/1000/gvfs/smb-share:server=172.21.251.64,share=nas/tools/TPTBox/broken.pkl", "rb") as w:
        brocken = pickle.load(w)
    for bf in brocken:
        bf: BIDS_FILE
        # Localize the zip
        subj = bf.get("sub")
        mod = bf.format
        sub_key = bf.get(source_folders[mod]["rule"])
        print(source_folders[mod].keys(), sub_key)
        assert sub_key is not None, mod
        zip_file = next((source_folder_encrypted / source_folders[mod][sub_key]).glob(f"{subj}*.zip"))
        # Call the extraction
        out_files = extract_folder(zip_file, Path(out_folder), make_subject_chunks=3)
        # -- Testing ---
        # Save over brocken...
        for o in out_files.values():
            if test_nii(o):
                print("Still Broken ", out_files)


if __name__ == "__main__":
    find_all_broken()
    # test_and_replace(
    #    "/DATA/NAS/datasets_processed/NAKO/dataset-nako/rawdata/101/101776/mevibe/sub-101776_sequ-94_acq-ax_part-eco0-opp1_mevibe.nii.gz",
    #    "/DATA/NAS/datasets_processed/NAKO/dataset-nako/",
    # )
    # extract_folder(
    #    Path(
    #        "/DATA/NAS/datasets_source/epi/NAKO/NAKO_2D_issue/ME_vibe_fatquant_pre_Eco0_OPP1/111177_30_ME_vibe_fatquant_pre_Eco0_OPP1.zip"
    #    ),
    #    Path("/DATA/NAS/datasets_source/epi/NAKO/dataset-nako2"),
    # )

# ['python', '/DATA/NAS/tools/spineps/spineps/example/helper_parallel.py', '-i', '/DATA/NAS/datasets_processed/NAKO/dataset-nako/rawdata/101/101509/mevibe/sub-101509_sequ-77_acq-ax_part-eco0-opp1_mevibe.nii.gz', '-ds',
# '/DATA/NAS/datasets_processed/NAKO/dataset-nako', '-der', 'derivatives_mevibe', '-ms', 'vibe', '-snap', '/DATA/NAS/datasets_processed/NAKO/dataset-nako/derivatives_mevibe/snap']
# [*] Command called with args: ['python', '/DATA/NAS/tools/spineps/spineps/example/helper_parallel.py', '-i', '/DATA/NAS/datasets_processed/NAKO/dataset-nako/rawdata/101/101509/mevibe/sub-101509_sequ-77_acq-ax_part-ec
# o0-opp1_mevibe.nii.gz', '-ds', '/DATA/NAS/datasets_processed/NAKO/dataset-nako', '-der', 'derivatives_mevibe', '-ms', 'vibe', '-snap', '/DATA/NAS/datasets_processed/NAKO/dataset-nako/derivatives_mevibe/snap']
# [*] Fold [0]:  takes free gpu 0
# ['python', '/DATA/NAS/tools/spineps/spineps/example/helper_parallel.py', '-i', '/DATA/NAS/datasets_processed/NAKO/dataset-nako/rawdata/101/101776/mevibe/sub-101776_sequ-94_acq-ax_part-eco0-opp1_mevibe.nii.gz', '-ds',
# '/DATA/NAS/datasets_processed/NAKO/dataset-nako', '-der', 'derivatives_mevibe', '-ms', 'vibe', '-snap', '/DATA/NAS/datasets_processed/NAKO/dataset-nako/derivatives_mevibe/snap']
# [*] Command called with args: ['python', '/DATA/NAS/tools/spineps/spineps/example/helper_parallel.py', '-i', '/DATA/NAS/datasets_processed/NAKO/dataset-nako/rawdata/101/101776/mevibe/sub-101776_sequ-94_acq-ax_part-ec
# o0-opp1_mevibe.nii.gz', '-ds', '/DATA/NAS/datasets_processed/NAKO/dataset-nako', '-der', 'derivatives_mevibe', '-ms', 'vibe', '-snap', '/DATA/NAS/datasets_processed/NAKO/dataset-nako/derivatives_mevibe/snap']
# [*] Fold [0]:  takes free gpu 0
# ['python', '/DATA/NAS/tools/spineps/spineps/example/helper_parallel.py', '-i', '/DATA/NAS/datasets_processed/NAKO/dataset-nako/rawdata/101/101765/mevibe/sub-101765_sequ-53_acq-ax_part-eco0-opp1_mevibe.nii.gz', '-ds',
# '/DATA/NAS/datasets_processed/NAKO/dataset-nako', '-der', 'derivatives_mevibe', '-ms', 'vibe', '-snap', '/DATA/NAS/datasets_processed/NAKO/dataset-nako/derivatives_mevibe/snap']
# [*] Command called with args: ['python', '/DATA/NAS/tools/spineps/spineps/example/helper_parallel.py', '-i', '/DATA/NAS/datasets_processed/NAKO/dataset-nako/rawdata/101/101765/mevibe/sub-101765_sequ-53_acq-ax_part-ec
# o0-opp1_mevibe.nii.gz', '-ds', '/DATA/NAS/datasets_processed/NAKO/dataset-nako', '-der', 'derivatives_mevibe', '-ms', 'vibe', '-snap', '/DATA/NAS/datasets_processed/NAKO/dataset-nako/derivatives_mevibe/snap']
