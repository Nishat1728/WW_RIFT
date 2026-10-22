// Exercise the ACTUAL integration reader/writer with host-memory CUDA API stubs.
// This verifies binary serialization, not CUDA integration/kernel equivalence.
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <memory>
#include <sstream>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>
#include <cassert>

constexpr int DtoH=0, HtoD=1, GrowGridSize=2;
constexpr std::size_t pitch=6*sizeof(double);
int cudaMemcpy(void* dst, const void* src, std::size_t n, int) { std::memcpy(dst,src,n); return 0; }
int cudaMemcpy2D(void* dst, std::size_t dp, const void* src, std::size_t sp,
                 std::size_t width, std::size_t height, int) {
    for (std::size_t row=0; row<height; ++row)
        std::memcpy(static_cast<char*>(dst)+row*dp, static_cast<const char*>(src)+row*sp,width);
    return 0;
}
int cudaMemset2D(void* dst, std::size_t dp, int value, std::size_t width, std::size_t height) {
    for (std::size_t row=0; row<height; ++row) std::memset(static_cast<char*>(dst)+row*dp,value,width);
    return 0;
}
#define checkCudaErrors(call) do { if ((call)!=0) throw std::runtime_error("stub copy failed"); } while(0)
struct MockGrid {
    int h_nx=6,h_ny=6,b_nx=3,b_ny=3;
    void write(std::ostream& out) {
        out.write(reinterpret_cast<char*>(&b_nx),4); out.write(reinterpret_cast<char*>(&b_ny),4);
    }
};
struct Simulator {
    bool infiltration=true,antecedent_soil=true,dynamic_soil_saturation=true,urban_drainage=true;
    bool h_print=true,q_print=true,save_max=true,save_arrival_time=true,restart_reset_diagnostics=false;
    std::string output_file,restart_write_path;
    int count_print=0;
    double t=40,dt=0.25,t0=40,tf=80;
    double V_added=10,V_computed=20,initial_storage_depth=3,volume_old=4;
    bool mass_reference_initialized=true;
    MockGrid grid_config;
    bool dev_wet_blocks[2]={true,false};
    int dev_active_blocks[2]={1,0};
    double dev_w[36]{},dev_hu[36]{},dev_hv[36]{},dev_F[36]{},dev_soil_saturation[36]{},dev_dtheta[36]{};
    double dev_urban_storage[36]{},dev_urban_cumulative_outflow[36]{},dev_urban_peak_capture[36]{},dev_urban_peak_release[36]{};
    double dev_h[36]{},dev_q[36]{},dev_h_max[36]{},dev_q_max[36]{},dev_time_peak[36]{},dev_time_dry[36]{},dev_t_wet[36]{};
    std::vector<std::pair<std::string,double*>> RestartFields();
    std::uint32_t RestartFlags() const;
    void WriteHotstart();
    void ReadHotstart(const std::string&);
};
#include "complete_restart.inc"

int main(int argc,char** argv) {
    if (argc!=2) return 2;
    Simulator original;
    original.output_file=argv[1];
    auto fields=original.RestartFields();
    for (std::size_t j=0;j<fields.size();++j)
        for (int i=0;i<36;++i) fields[j].second[i]=double(j+1)+i*0.001;
    original.WriteHotstart();
    const std::string file=original.output_file+"/hotstart_0000.bin";
    Simulator restored;
    restored.ReadHotstart(file);
    auto copied=restored.RestartFields();
    for (std::size_t j=0;j<fields.size();++j)
        assert(std::memcmp(fields[j].second,copied[j].second,36*sizeof(double))==0);
    assert(restored.V_added==10 && restored.V_computed==20 && restored.initial_storage_depth==3);
    assert(restored.t==40 && restored.dt==0.25 && restored.dev_wet_blocks[0] && restored.dev_active_blocks[0]==1);
    restored.restart_reset_diagnostics=true;
    restored.ReadHotstart(file);
    assert(restored.dev_F[0]==original.dev_F[0] && restored.dev_soil_saturation[0]==original.dev_soil_saturation[0]);
    assert(restored.dev_hu[0]==original.dev_hu[0] && restored.dev_urban_storage[0]==original.dev_urban_storage[0]);
    assert(restored.dev_h_max[0]==0 && restored.dev_urban_cumulative_outflow[0]==0 && restored.V_added==0);
    restored.grid_config.b_nx=4;
    bool rejected=false;
    try { restored.ReadHotstart(file); } catch(const std::runtime_error&) { rejected=true; }
    assert(rejected);
    std::cout << "NATIVE_COMPLETE_STATE_ROUNDTRIP_PASSED\n";
}
