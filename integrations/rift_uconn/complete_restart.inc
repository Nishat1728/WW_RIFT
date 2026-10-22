// WW_RIFT complete-restart contract v1. Included at the end of simulator.cu.
// Static terrain/parameters are reloaded from identical case inputs. Forcing is
// selected for the new phase; it is intentionally not restored from the old one.
#include <cstdint>
#include <cstring>
#include <cstdio>
#include <algorithm>

namespace ww_restart {
template<class T> void put(std::ostream& out, const T& value) {
    out.write(reinterpret_cast<const char*>(&value), sizeof(T));
    if (!out) throw std::runtime_error("Cannot write complete hotstart");
}
template<class T> T get(std::istream& in) {
    T value{};
    in.read(reinterpret_cast<char*>(&value), sizeof(T));
    if (!in) throw std::runtime_error("Truncated complete hotstart");
    return value;
}
void put_string(std::ostream& out, const std::string& text) {
    put(out, static_cast<std::uint32_t>(text.size()));
    out.write(text.data(), text.size());
}
std::string get_string(std::istream& in) {
    auto length = get<std::uint32_t>(in);
    if (length > 4096) throw std::runtime_error("Invalid hotstart string length");
    std::string text(length, '\0');
    in.read(&text[0], length);
    if (!in) throw std::runtime_error("Truncated hotstart string");
    return text;
}
}

std::vector<std::pair<std::string, double*>> Simulator::RestartFields() {
    std::vector<std::pair<std::string, double*>> fields = {
        {"water_surface", dev_w}, {"momentum_x", dev_hu}, {"momentum_y", dev_hv}
    };
    if (infiltration) fields.push_back({"cumulative_infiltration", dev_F});
    if (antecedent_soil) {
        fields.push_back({"soil_saturation", dev_soil_saturation});
        fields.push_back({"moisture_deficit", dev_dtheta});
    }
    if (urban_drainage) {
        fields.push_back({"urban_storage", dev_urban_storage});
        fields.push_back({"urban_cumulative_outflow", dev_urban_cumulative_outflow});
        fields.push_back({"urban_peak_capture", dev_urban_peak_capture});
        fields.push_back({"urban_peak_release", dev_urban_peak_release});
    }
    if (h_print) fields.push_back({"water_depth", dev_h});
    if (q_print) fields.push_back({"unit_discharge", dev_q});
    if (save_max) {
        fields.push_back({"peak_depth", dev_h_max});
        fields.push_back({"peak_discharge", dev_q_max});
        fields.push_back({"time_peak", dev_time_peak});
        fields.push_back({"time_dry", dev_time_dry});
    }
    if (save_arrival_time) fields.push_back({"arrival_time", dev_t_wet});
    return fields;
}

std::uint32_t Simulator::RestartFlags() const {
    return (infiltration ? 1 : 0) | (antecedent_soil ? 2 : 0)
        | (dynamic_soil_saturation ? 4 : 0) | (urban_drainage ? 8 : 0)
        | (h_print ? 16 : 0) | (q_print ? 32 : 0)
        | (save_max ? 64 : 0) | (save_arrival_time ? 128 : 0);
}

void Simulator::WriteHotstart() {
    using namespace ww_restart;
    std::ostringstream name;
    name << output_file << "/hotstart_" << std::setw(4) << std::setfill('0') << count_print << ".bin";
    const std::string filename = restart_write_path.empty() ? name.str() : restart_write_path;
    const std::string pending = filename + ".pending";
    std::ofstream out(pending, std::ios::binary | std::ios::trunc);
    if (!out) throw std::runtime_error("Cannot open hotstart " + pending);
    out.write("WWRIFT01", 8);
    put(out, std::uint32_t(1));
    put(out, RestartFlags());
    put(out, t); put(out, dt);
    std::ostringstream geometry(std::ios::binary);
    grid_config.write(geometry);
    put_string(out, geometry.str());
    const auto fields = RestartFields();
    put(out, static_cast<std::uint32_t>(fields.size()));
    const std::uint64_t cells = std::uint64_t(grid_config.h_nx) * grid_config.h_ny;
    std::vector<double> buffer(cells);
    for (const auto& field : fields) {
        put_string(out, field.first); put(out, cells);
        checkCudaErrors(cudaMemcpy2D(buffer.data(), grid_config.h_nx*sizeof(double),
            field.second, pitch, grid_config.h_nx*sizeof(double), grid_config.h_ny, DtoH));
        out.write(reinterpret_cast<const char*>(buffer.data()), cells*sizeof(double));
        if (!out) throw std::runtime_error("Incomplete hotstart field " + field.first);
    }
    put(out, static_cast<std::uint64_t>(GrowGridSize));
    std::unique_ptr<bool[]> wet(new bool[GrowGridSize]);
    std::vector<int> active(GrowGridSize);
    checkCudaErrors(cudaMemcpy(wet.get(), dev_wet_blocks, GrowGridSize*sizeof(bool), DtoH));
    checkCudaErrors(cudaMemcpy(active.data(), dev_active_blocks, GrowGridSize*sizeof(int), DtoH));
    out.write(reinterpret_cast<const char*>(wet.get()), GrowGridSize*sizeof(bool));
    out.write(reinterpret_cast<const char*>(active.data()), GrowGridSize*sizeof(int));
    put(out, V_added); put(out, V_computed); put(out, initial_storage_depth);
    put(out, volume_old); put(out, mass_reference_initialized);
    if (!out) throw std::runtime_error("Incomplete hotstart block state");
    out.flush(); out.close();
    if (!out || std::rename(pending.c_str(), filename.c_str()))
        throw std::runtime_error("Cannot publish complete hotstart " + filename);
    std::ofstream meta(filename + ".json.pending");
    meta << std::setprecision(17)
         << "{\n  \"schema\":\"rift-uconn-complete-restart-v1\",\n"
         << "  \"simulation_time_s\":" << t << ",\n  \"timestep_s\":" << dt
         << ",\n  \"flags\":" << RestartFlags()
         << ",\n  \"node_columns\":" << grid_config.b_nx
         << ",\n  \"node_rows\":" << grid_config.b_ny
         << ",\n  \"fields\":[";
    for (std::size_t i=0; i<fields.size(); ++i) {
        if (i) meta << ",";
        meta << "\"" << fields[i].first << "\"";
    }
    meta << "]\n}\n";
    meta.flush(); meta.close();
    if (!meta || std::rename((filename+".json.pending").c_str(), (filename+".json").c_str()))
        throw std::runtime_error("Cannot publish hotstart metadata");
}

void Simulator::ReadHotstart(const std::string& filename) {
    using namespace ww_restart;
    std::ifstream in(filename, std::ios::binary);
    if (!in) throw std::runtime_error("Cannot open hotstart " + filename);
    char magic[8]{};
    in.read(magic, 8);
    if (std::memcmp(magic, "WWRIFT01", 8) || get<std::uint32_t>(in) != 1)
        throw std::runtime_error("Legacy/unknown hotstart is not a complete WW_RIFT restart; regenerate spin-up with the patched solver");
    if (get<std::uint32_t>(in) != RestartFlags())
        throw std::runtime_error("Hotstart physics/output state flags differ from this configuration");
    const double saved_t = get<double>(in), saved_dt = get<double>(in);
    if (!std::isfinite(saved_t) || !std::isfinite(saved_dt) || saved_dt <= 0
        || std::abs(saved_t-t0) > 1.e-6 || saved_t >= tf)
        throw std::runtime_error("Hotstart time must equal simulation.start_time_s and precede its end");
    std::ostringstream geometry(std::ios::binary);
    grid_config.write(geometry);
    if (get_string(in) != geometry.str())
        throw std::runtime_error("Hotstart grid extent/geometry differs from the current grid");
    const auto fields = RestartFields();
    if (get<std::uint32_t>(in) != fields.size()) throw std::runtime_error("Hotstart field count mismatch");
    const std::uint64_t cells = std::uint64_t(grid_config.h_nx) * grid_config.h_ny;
    std::vector<double> buffer(cells);
    for (const auto& field : fields) {
        if (get_string(in) != field.first || get<std::uint64_t>(in) != cells)
            throw std::runtime_error("Hotstart named field/dimension mismatch");
        in.read(reinterpret_cast<char*>(buffer.data()), cells*sizeof(double));
        if (!in || !std::all_of(buffer.begin(), buffer.end(), [](double x){return std::isfinite(x);}))
            throw std::runtime_error("Truncated/non-finite hotstart state " + field.first);
        checkCudaErrors(cudaMemcpy2D(field.second, pitch, buffer.data(), grid_config.h_nx*sizeof(double),
            grid_config.h_nx*sizeof(double), grid_config.h_ny, HtoD));
    }
    if (get<std::uint64_t>(in) != std::uint64_t(GrowGridSize)) throw std::runtime_error("Hotstart active grid mismatch");
    std::unique_ptr<bool[]> wet(new bool[GrowGridSize]);
    std::vector<int> active(GrowGridSize);
    in.read(reinterpret_cast<char*>(wet.get()), GrowGridSize*sizeof(bool));
    in.read(reinterpret_cast<char*>(active.data()), GrowGridSize*sizeof(int));
    V_added = get<double>(in); V_computed = get<double>(in);
    initial_storage_depth = get<double>(in); volume_old = get<double>(in);
    mass_reference_initialized = get<bool>(in);
    if (in.peek() != std::char_traits<char>::eof()) throw std::runtime_error("Unexpected hotstart trailing bytes");
    checkCudaErrors(cudaMemcpy(dev_wet_blocks, wet.get(), GrowGridSize*sizeof(bool), HtoD));
    checkCudaErrors(cudaMemcpy(dev_active_blocks, active.data(), GrowGridSize*sizeof(int), HtoD));
    t = saved_t; dt = saved_dt;
    if (restart_reset_diagnostics) {
        // Reset event statistics only. Never reset physical infiltration, soil
        // saturation, moisture deficit, momentum or urban retained water.
        const auto clear = [&](double* ptr) {
            checkCudaErrors(cudaMemset2D(ptr, pitch, 0, grid_config.h_nx*sizeof(double), grid_config.h_ny));
        };
        if (save_max) { clear(dev_h_max); clear(dev_q_max); clear(dev_time_peak); clear(dev_time_dry); }
        if (save_arrival_time) clear(dev_t_wet);
        if (urban_drainage) { clear(dev_urban_cumulative_outflow); clear(dev_urban_peak_capture); clear(dev_urban_peak_release); }
        V_added = V_computed = initial_storage_depth = volume_old = 0;
        mass_reference_initialized = false;
    }
    std::cout << "[restart] schema=rift-uconn-complete-restart-v1 time_s=" << std::setprecision(17)
              << t << " fields=" << fields.size() << " diagnostics_reset=" << restart_reset_diagnostics << std::endl;
}
